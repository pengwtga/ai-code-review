"""引擎：diff → 上下文 → 判据检测 → 去重聚合 → ReviewResult。

- 判据即白名单：只跑注册过的检测器（detectors.DETERMINISTIC_DETECTORS + L2）。
- 去重规则（M0 第 2 节）：同文件行号差 ≤3 的多判据命中，保留 severity/confidence 最高者，
  其余判据 ID 挂到主 finding 的 also_flags，不重复评论。
- 置信度阈值以下的 finding 不输出（宁可少报）。
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .context import FileContext
from .criteria_schema import Finding, ReviewStats, SEVERITY_ORDER
from .detectors import DETERMINISTIC_DETECTORS
from .diff_parser import FileDiff, parse_unified_diff, changed_line_numbers, make_unified_diff


@dataclass
class ReviewResult:
    findings: list[Finding]
    stats: ReviewStats

    def to_dict(self) -> dict:
        return {
            "criteria_version": self.stats.criteria_version,
            "mode": self.stats.mode,
            "stats": {
                "files_reviewed": self.stats.files_reviewed,
                "findings": self.stats.findings,
                "by_criterion": self.stats.by_criterion,
                "est_cost_cny": self.stats.est_cost_cny,
                "agent": self.stats.agent,
            },
            "findings": [f.to_dict() for f in self.findings],
        }


class ReviewEngine:
    def __init__(self, confidence_threshold: float = 0.55,
                 detectors: list | None = None,
                 enable_llm: bool = False):
        self.confidence_threshold = confidence_threshold
        self.detectors = detectors if detectors is not None else list(DETERMINISTIC_DETECTORS)
        self.enable_llm = enable_llm

    # ---- 入口 1：前后文本（评测夹具 / git show） -------------------------------
    def review(self, old_files: dict[str, str], new_files: dict[str, str]) -> ReviewResult:
        diff_text = make_unified_diff(old_files, new_files)
        fds = parse_unified_diff(diff_text)
        return self._review(fds, {**old_files, **new_files}, llm_ready=True)

    # ---- 入口 2：现成 diff（webhook 场景） ------------------------------------
    def review_diff(self, diff_text: str, repo: Path | None = None) -> ReviewResult:
        fds = parse_unified_diff(diff_text)
        sources: dict[str, str] = {}
        for fd in fds:
            path = fd.new_path or fd.old_path
            if repo is not None:
                p = repo / path
                if p.exists():
                    sources[path] = p.read_text(encoding="utf-8", errors="replace")
            if path not in sources:
                sources[path] = _reconstruct_new(fd)
        return self._review(fds, sources, llm_ready=False)

    # ---- 内部 ------------------------------------------------------------------
    def _review(self, fds: list[FileDiff], sources: dict[str, str],
                llm_ready: bool) -> ReviewResult:
        raw: list[tuple[Finding, str]] = []   # (finding, scope) scope=enclosing function
        files_reviewed = 0
        llm_units: list[tuple] = []           # (fc, path, func_source, func_lineno, changed)
        contexts: dict[str, FileContext] = {}
        for fd in fds:
            path = fd.new_path or fd.old_path
            if not path.endswith(".py"):
                continue
            src = sources.get(path, "")
            fc = FileContext(path=path, new_source=src, changed_lines=changed_line_numbers(fd))
            contexts[path] = fc
            if fc.tree.body:
                files_reviewed += 1
                for det in self.detectors:
                    for f in det(fc):
                        raw.append((f, _scope(fc, f.line)))
                if llm_ready and self.enable_llm:
                    # L2 单元 = 变更行所在的 enclosing function（模块级变更不进 L2）
                    by_scope: dict[str, list[int]] = {}
                    for ln in sorted(fc.changed_lines):
                        func = fc.function_at(ln)
                        key = func.name if func is not None else "<module>"
                        by_scope.setdefault(key, []).append(ln)
                    for key, lines in by_scope.items():
                        if key == "<module>":
                            continue
                        func = fc.function_by_name(key)
                        llm_units.append((fc, path, fc.function_source(func), func.lineno, lines))

        agent_stats: dict = {}
        if llm_units:
            raw_llm, agent_stats = self._run_llm(llm_units, sources)
            raw.extend(raw_llm)

        # PR 级聚合检测（N1/N4/M1）：需要全部文件的上下文，在逐文件检测之后
        from .pr_detectors import run_pr_detectors
        raw.extend(run_pr_detectors(fds, contexts, sources))

        findings = self._dedup(raw)
        findings = [f for f in findings if f.confidence >= self.confidence_threshold]
        findings.sort(key=lambda f: f.sort_key())

        by_c: dict[str, int] = {}
        for f in findings:
            by_c[f.criterion_id] = by_c.get(f.criterion_id, 0) + 1
        mode = "hybrid" if agent_stats.get("units") else "deterministic"
        stats = ReviewStats(files_reviewed=files_reviewed, findings=len(findings),
                            by_criterion=by_c, mode=mode, agent=agent_stats)
        return ReviewResult(findings=findings, stats=stats)

    def _run_llm(self, units: list[tuple], sources: dict[str, str]) -> tuple[list[tuple], dict]:
        """受控 agent 循环：每单元独立工具预算（8 次），全程 trace 汇总进 stats。"""
        from . import llm as llm_mod
        from .tools import ReviewToolbox
        out: list[tuple[Finding, str]] = []
        agg: dict = {"units": 0, "tool_calls": 0, "iterations": 0, "errors": 0, "max_calls_per_unit": 8}
        for fc, path, func_src, func_lineno, lines in units:
            toolbox = ReviewToolbox(sources=sources, max_calls=agg["max_calls_per_unit"])
            try:
                fs, trace = llm_mod.agent_review_unit(path, func_src, func_lineno, lines, toolbox)
            except RuntimeError as e:
                print(f"[aicr] L2 降级为确定性层：{e}", file=sys.stderr)
                return out, {**agg, "degraded": True}
            except Exception as e:                      # 单元失败不炸整体审查
                agg["errors"] += 1
                print(f"[aicr] L2 单元失败（{path}）: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            agg["units"] += 1
            agg["tool_calls"] += trace["tool_calls"]
            agg["iterations"] += trace["iterations"]
            if trace.get("rejected_by_gate"):
                agg["rejected_by_gate"] = agg.get("rejected_by_gate", 0) + trace["rejected_by_gate"]
            for f in fs:
                out.append((f, _scope(fc, f.line)))
        return out, agg

    def _dedup(self, raw: list[tuple[Finding, str]]) -> list[Finding]:
        """同文件 + 同函数 + 行距 ≤3 才算重复（跨函数的两个问题不是重复）。"""
        raw = sorted(raw, key=lambda t: t[0].sort_key())
        kept: list[Finding] = []
        for f, scope in raw:
            merged = False
            for k in kept:
                if k.file == f.file and getattr(k, "_scope", "") == scope \
                        and abs(k.line - f.line) <= 3:
                    if f.criterion_id not in k.also_flags:
                        k.also_flags.append(f.criterion_id)
                    merged = True
                    break
            if not merged:
                f._scope = scope          # 内部标记，输出前仍保留（to_dict 不含）
                kept.append(f)
        return kept


def _reconstruct_new(fd: FileDiff) -> str:
    """diff-only 且无 repo 时的降级路径：用 hunk 内上下文行 + 新增行拼稀疏全文。"""
    lines: dict[int, str] = {}
    for hunk in fd.hunks:
        for l in hunk.lines:
            if l.kind in (" ", "+") and l.new_lineno is not None:
                lines[l.new_lineno] = l.text
    if not lines:
        return ""
    out = []
    for i in range(1, max(lines) + 1):
        out.append(lines.get(i, ""))
    return "\n".join(out)


def _scope(fc: FileContext, line: int) -> str:
    func = fc.function_at(line)
    return func.name if func is not None else "<module>"
