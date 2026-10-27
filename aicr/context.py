"""上下文装配：全文 AST → 函数级上下文。

判据文档第 2 节：C/E 路的装配单位是「变更单元 + 所在函数完整体」。
本模块负责：定位变更行 → 找到 enclosing function → 提供函数全文与 AST 节点。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field


@dataclass
class FileContext:
    path: str
    new_source: str
    changed_lines: set[int]          # 新文件侧的变更行号（新增行）
    tree: ast.Module = field(init=False)
    _func_by_name: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(
        default_factory=dict, init=False)

    def __post_init__(self) -> None:
        try:
            self.tree = ast.parse(self.new_source)
        except SyntaxError:
            self.tree = ast.Module(body=[], type_ignores=[])
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._func_by_name.setdefault(node.name, node)

    def function_at(self, lineno: int) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        """返回包含该行的最内层函数（不含嵌套函数定义内部再嵌套的精确处理，v0 够用）。"""
        best = None
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.lineno <= lineno <= getattr(node, "end_lineno", node.lineno):
                    if best is None or node.lineno > best.lineno:
                        best = node
        return best

    def function_source(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.new_source, node) or ""

    def function_by_name(self, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        return self._func_by_name.get(name)

    def is_changed(self, lineno: int | None) -> bool:
        return lineno is not None and lineno in self.changed_lines


def annotation_allows_none(node: ast.expr) -> bool:
    """判断返回注解是否可能为 None：Optional[X] / X | None / None 联合。"""
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        return False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_none_name(node.left) or _is_none_name(node.right) \
            or annotation_allows_none(node.left) or annotation_allows_none(node.right)
    if isinstance(node, ast.Subscript):
        # typing.Optional[X] / Union[X, None]
        base = node.value
        name = base.id if isinstance(base, ast.Name) else ""
        if name in ("Optional", "Union"):
            for elt in node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]:
                if _is_none_name(elt) or annotation_allows_none(elt):
                    return True
        return False
    return False


def _is_none_name(node: ast.expr) -> bool:
    if isinstance(node, ast.Name) and node.id == "None":
        return True
    return isinstance(node, ast.Constant) and node.value is None
