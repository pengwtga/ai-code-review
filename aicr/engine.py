"""引擎：diff → 上下文 → 判据检测 → 去重聚合 → ReviewResult。

- 判据即白名单：只跑注册过的检测器（detectors.DETERMINISTIC_DETECTORS + L2）。
- 去重规则（M0 第 2 节）：同文件行号差 ≤3 的多判据命中，保留 severity/confidence 最高者，
  其余判据 ID 挂到主 finding 的 also_flags，不重复评论。
- 置信度阈值以下的 finding 不输出（宁可少报）。
"""

from __future__ import annotations

import ast
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
        for fd in fds:
            path = fd.new_path or fd.old_path
            if not path.endswith(".py"):
                continue
            src = sources.get(path, "")
            fc = FileContext(path=path, new_source=src, changed_lines=changed_line_numbers(fd))
            if fc.tree.body:
                files_reviewed += 1
                for det in self.detectors:
                    for f in det(fc):
                        raw.append((f, _scope(fc, f.line)))

        findings = self._dedup(raw)
        findings = [f for f in findings if f.confidence >= self.confidence_threshold]
        findings.sort(key=lambda f: f.sort_key())

        by_c: dict[str, int] = {}
        for f in findings:
            by_c[f.criterion_id] = by_c.get(f.criterion_id, 0) + 1
        stats = ReviewStats(files_reviewed=files_reviewed, findings=len(findings),
                            by_criterion=by_c, mode="deterministic")
        return ReviewResult(findings=findings, stats=stats)

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
