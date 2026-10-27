"""PR 级聚合检测器（M1.5：N1 / N4 / M1）。

与逐文件检测器的本质区别：需要跨文件的 PR 全局视角——
- N1（招牌判据）：同 PR 同构改动不一致。单看任何 hunk 都正确，
  问题只存在于横向对比里。这是逐 hunk 工具（含 Anthropic 式）架构上拿不到的证据。
- N4：定义在本 PR 被删/改名，同 PR 新增行仍引用旧名。
- M1：本 PR 删除了 assert 且新文件全文无 assert（静默放宽既有约束，保守形态）。

精度偏保守（v0）：N1 要求聚类 ≥2、相似度 ≥0.5、目标函数全文确实缺失该属性；
M1 要求全文零 assert。已知局限见各函数 docstring。
"""

from __future__ import annotations

import ast
import re

from .context import FileContext
from .criteria_schema import Finding
from .diff_parser import FileDiff

_DEF_RE = re.compile(r"^\s*(?:def\s+(\w+)\s*\(|class\s+(\w+)\b)")
_MIN_CLUSTER = 2
_SIM_THRESHOLD = 0.5


class _Unit:
    """一个变更函数：PR 级检测的原子。"""

    def __init__(self, path: str, func: ast.AST, added_texts: list[str],
                 old_context_text: str, fc: FileContext):
        self.path = path
        self.func = func
        self.name = func.name
        self.lineno = func.lineno
        self.end = getattr(func, "end_lineno", func.lineno)
        self.added_texts = added_texts
        self.old_context_text = old_context_text
        self.fc = fc
        self.full_text = fc.function_source(func)
        self.profile = _func_call_profile(func)          # 新函数体的方法/属性轮廓
        call_names = set(re.findall(r"\.(\w+)\s*\(", "\n".join(added_texts)))
        # 新增行上的属性访问（去掉方法调用位置），作为 N1 的判别特征
        self.added_attrs = set(re.findall(r"\b\w+\.(\w+)", "\n".join(added_texts))) \
            - call_names


def _func_call_profile(tree_node: ast.AST) -> set[str]:
    """函数体内的方法名 + 属性名 + 被调名（相似度特征）。"""
    names: set[str] = set()
    for node in ast.walk(tree_node):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _collect_units(fds: list[FileDiff],
                   contexts: dict[str, FileContext]) -> list[_Unit]:
    """变更行 → enclosing function，同时保留 hunk 行文本（新增 + 旧上下文）。"""
    units: list[_Unit] = []
    for fd in fds:
        path = fd.new_path or fd.old_path
        fc = contexts.get(path)
        if fc is None:
            continue
        added: dict[int, list[str]] = {}      # 新行号 → 行文本
        old_text: list[str] = []
        for hunk in fd.hunks:
            for l in hunk.lines:
                if l.kind == "+":
                    added[l.new_lineno or 0] = added.get(l.new_lineno or 0, []) + [l.text]
                elif l.kind in (" ", "-"):
                    old_text.append(l.text)
        grouped: dict[int, list] = {}         # 函数起始行 → [func, added_texts]
        for ln, texts in added.items():
            func = fc.function_at(ln)
            if func is None:
                continue                      # 模块级变更不进 N 路
            slot = grouped.setdefault(func.lineno, [func, []])
            slot[1].extend(texts)
        for func, texts in grouped.values():
            units.append(_Unit(path, func, texts, "\n".join(old_text), fc))
    return units


def detect_n1(units: list[_Unit]) -> list[Finding]:
    """同 PR 同构改动不一致：属性 T 在 ≥2 个变更函数中新增，同构函数 G 缺失。

    反例（不报）：
    - G 全文或旧上下文已含 T（本就有，或非本 PR 引入的差异）；
    - G 与聚类成员的调用轮廓 Jaccard < 0.5（不同构，如内部只读路由）；
    - 聚类成员 < 2（两处差异构不成"模式"）。
    已知局限：G 缺 T 的上下文解释（如内部路由标注）在 diff 里看不见时，
    由置信度 0.7 + 人工确认兜底——N1 的产出定位是"高度可疑、值得人看"。
    """
    findings: list[Finding] = []
    attrs_index: dict[str, list[_Unit]] = {}
    for u in units:
        for t in u.added_attrs:
            attrs_index.setdefault(t, []).append(u)

    for t, cluster in attrs_index.items():
        if len(cluster) < _MIN_CLUSTER:
            continue
        member_refs = [f"{u.path}:{u.lineno}" for u in cluster]
        for g in units:
            if g in cluster:
                continue
            if t in g.full_text or t in g.old_context_text:
                continue                        # G 已有该属性 → 不报
            if not g.added_texts:
                continue
            sim = max((_jaccard(g.profile, m.profile) for m in cluster), default=0.0)
            if sim < _SIM_THRESHOLD:
                continue                        # 不同构 → 不报
            findings.append(Finding(
                criterion_id="N1", file=g.path, line=g.lineno,
                severity="major", confidence=0.7,
                message=(f"本 PR 在 {len(cluster)} 个同构位置统一增加了 .{t} "
                         f"（如 {member_refs[0]}），{g.name} 操作同类对象但全文缺少 "
                         f"{t}——疑似同构改动遗漏。"),
                evidence=[f"{g.path}:{g.lineno}-{g.end}"] + member_refs,
            ))
    return findings


def detect_n4(fds: list[FileDiff], new_sources: dict[str, str]) -> list[Finding]:
    """变更不完整：def/class 在本 PR 被删，同 PR 新增行仍引用旧名。"""
    removed: dict[str, str] = {}                  # name → 原位置
    for fd in fds:
        path = fd.new_path or fd.old_path
        for hunk in fd.hunks:
            for l in hunk.lines:
                if l.kind != "-":
                    continue
                m = _DEF_RE.match(l.text)
                if m and not (m.group(1) or m.group(2) or "").startswith("__"):
                    removed.setdefault(m.group(1) or m.group(2),
                                       f"{path}:{l.old_lineno or 0}")
    if not removed:
        return []
    all_new = "\n".join(new_sources.values())
    findings: list[Finding] = []
    for name, origin in removed.items():
        pat = re.escape(name)
        if re.search(rf"^\s*def\s+{pat}\s*\(", all_new, re.M) \
                or re.search(rf"^\s*class\s+{pat}\b", all_new, re.M):
            continue                            # 新源仍有定义（如包装函数），不报
        for fd in fds:
            path = fd.new_path or fd.old_path
            for l in fd.added_lines:
                if re.search(rf"\b{pat}\b", l.text):
                    findings.append(Finding(
                        criterion_id="N4", file=path, line=l.new_lineno or 0,
                        severity="major", confidence=0.75,
                        message=(f"{name} 的定义已在本 PR 删除/改名（原 {origin}），"
                                 "但同 PR 新增代码仍引用旧名——疑似改名未同步。"),
                        evidence=[f"{path}:{l.new_lineno}", origin],
                    ))
                    break
    return findings


def detect_m1(fds: list[FileDiff], new_sources: dict[str, str]) -> list[Finding]:
    """静默放宽既有约束（保守形态）：本 PR 删 assert 且新文件全文无 assert。

    只报最强的文件级形态以控噪音：断言移动/收窄等中间形态交 LLM 路或人工。
    """
    findings: list[Finding] = []
    for fd in fds:
        path = fd.new_path or fd.old_path
        removed_asserts = [l for h in fd.hunks for l in h.lines
                           if l.kind == "-" and re.search(r"\bassert\s+", l.text)]
        if not removed_asserts:
            continue
        if "assert" in new_sources.get(path, ""):
            continue                            # 文件里仍有 assert（移动/新增），不报
        line = removed_asserts[0].old_lineno or 0
        findings.append(Finding(
            criterion_id="M1", file=path, line=line,
            severity="major", confidence=0.65,
            message="本 PR 删除了 assert 断言且文件中已无任何断言——若这是校验逻辑"
                    "的静默放宽（而非迁到别处），调用方将失去该约束保护。",
            evidence=[f"{path}:{line}"],
        ))
    return findings


def run_pr_detectors(fds: list[FileDiff], contexts: dict[str, FileContext],
                     new_sources: dict[str, str]) -> list[tuple[Finding, str]]:
    units = _collect_units(fds, contexts)
    out: list[tuple[Finding, str]] = []
    for f in detect_n1(units):
        out.append((f, _scope_of(contexts, f.file, f.line)))
    for f in detect_n4(fds, new_sources):
        out.append((f, "<module>"))
    for f in detect_m1(fds, new_sources):
        out.append((f, "<module>"))
    return out


def _scope_of(contexts: dict[str, FileContext], path: str, line: int) -> str:
    fc = contexts.get(path)
    if fc is None:
        return "<module>"
    func = fc.function_at(line)
    return func.name if func else "<module>"
