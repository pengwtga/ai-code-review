"""Unified diff 解析：git diff / difflib 产物 → FileDiff / Hunk / 行级锚点。

设计约束：不引入第三方依赖；解析器只负责行号锚定，
新文件全文内容由调用方提供（git show / difflib 前后文本），
AST 分析永远基于全文而非 hunk 局部——这是 C/E 路判据的上下文前提。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class DiffLine:
    kind: str  # ' ', '+', '-'
    text: str
    old_lineno: int | None = None
    new_lineno: int | None = None


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def added_lines(self) -> list[DiffLine]:
        return [l for l in self.lines if l.kind == "+"]


@dataclass
class FileDiff:
    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def added_lines(self) -> list[DiffLine]:
        return [l for h in self.hunks for l in h.added_lines]

    @property
    def is_new_file(self) -> bool:
        return self.old_path in ("/dev/null", "")


def parse_unified_diff(diff_text: str) -> list[FileDiff]:
    """解析 git diff / difflib.unified_diff 输出。"""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    cur_hunk: Hunk | None = None
    old_ln = new_ln = 0

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            m = re.match(r'^diff --git a/(.*) b/(.*)$', raw)
            old_p, new_p = (m.group(1), m.group(2)) if m else ("", "")
            cur = FileDiff(old_path=old_p, new_path=new_p)
            files.append(cur)
            cur_hunk = None
            continue
        if raw.startswith("--- "):
            p = raw[4:].strip()
            if cur is not None:
                cur.old_path = _strip_path(p)
            continue
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            if cur is not None:
                cur.new_path = _strip_path(p)
            continue
        m = _HUNK_RE.match(raw)
        if m and cur is not None:
            old_ln = int(m.group(1))
            new_ln = int(m.group(3))
            cur_hunk = Hunk(old_start=old_ln, old_count=int(m.group(2) or 1),
                            new_start=new_ln, new_count=int(m.group(4) or 1))
            cur.hunks.append(cur_hunk)
            continue
        if cur_hunk is None or cur is None:
            continue
        if raw.startswith("+"):
            cur_hunk.lines.append(DiffLine("+", raw[1:], new_lineno=new_ln))
            new_ln += 1
        elif raw.startswith("-"):
            cur_hunk.lines.append(DiffLine("-", raw[1:], old_lineno=old_ln))
            old_ln += 1
        elif raw.startswith(" "):
            cur_hunk.lines.append(DiffLine(" ", raw[1:], old_lineno=old_ln, new_lineno=new_ln))
            old_ln += 1
            new_ln += 1
        elif raw.startswith("\\"):  # "\ No newline at end of file"
            continue
    return files


def _strip_path(p: str) -> str:
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p if p != "/dev/null" else ""


def make_unified_diff(old_files: dict[str, str], new_files: dict[str, str]) -> str:
    """测试/夹具用：由前后文本生成 unified diff（3 行上下文，与 git 默认一致）。"""
    import difflib

    out: list[str] = []
    for path in sorted(new_files):
        old = old_files.get(path, "")
        new = new_files.get(path, "")
        if old == new:
            continue
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        out.append(f"diff --git a/{path} b/{path}")
        out.append(f"--- a/{path}")
        out.append(f"+++ b/{path}")
        out.extend(list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=3))[2:])
    return "\n".join(out) + ("\n" if out else "")


def changed_line_numbers(fd: FileDiff) -> set[int]:
    """新增行的（新文件侧）行号集合——判据只允许报告这些行上的问题。"""
    return {l.new_lineno for l in fd.added_lines if l.new_lineno is not None}
