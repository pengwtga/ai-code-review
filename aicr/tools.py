"""L2 agent 只读工具集（M1.5）。

受控 agent 三原则（对齐「预算受限的受控 agent」差异化叙事）：
- 只读白名单：四个工具全部只读，无任何写路径；
- 作用域受限：只能读取本次变更涉及的文件集合（sources 的键），
  工具拿不到仓库其他文件——防止 agent 越界乱翻；
- 预算受控：每次 call() 计数，超预算返回错误 JSON 并拒绝执行，
  由 agent 循环据此强制收敛。

所有返回值是 JSON 字符串（工具协议），单次结果有长度上限，
防止一个大函数体把上下文预算吃穿。
"""

from __future__ import annotations

import ast
import json

_MAX_RESULTS = 20          # 每次工具调用最多返回条目
_MAX_RESULT_CHARS = 4000   # 单次返回截断长度


class ToolBudgetExceeded(Exception):
    pass


def _cap(text: str) -> str:
    if len(text) <= _MAX_RESULT_CHARS:
        return text
    return text[:_MAX_RESULT_CHARS] + f'\n...（截断，原长 {len(text)} 字符）'


class ReviewToolbox:
    """一次审查运行内的只读工具箱。sources: path -> 全文。"""

    TOOLS = ("find_symbol", "get_function_body", "list_callers", "search_repo")

    def __init__(self, sources: dict[str, str], max_calls: int = 8):
        self.sources = {p: s for p, s in sources.items() if p.endswith(".py")}
        self.max_calls = max_calls
        self.calls_made = 0
        self.trace: list[dict] = []          # 全程可回放：{tool, args, n_results, truncated}
        self._ast_cache: dict[str, ast.Module | None] = {}

    # ---- 协议 ---------------------------------------------------------------

    @property
    def spec(self) -> list[dict]:
        """OpenAI function-calling 格式的工具声明（白名单即声明，无隐式工具）。"""
        return [
            {"type": "function", "function": {
                "name": "find_symbol",
                "description": "按名字查找函数/类定义，返回定义位置列表。用于确认符号是否存在及其上下文。",
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "符号名（精确匹配）"}},
                    "required": ["name"]}}},
            {"type": "function", "function": {
                "name": "get_function_body",
                "description": "返回某文件某行所在函数（或模块级代码块）的源码全文。用于查看调用目标/被引用方的实现。",
                "parameters": {"type": "object", "properties": {
                    "file": {"type": "string"}, "line": {"type": "integer"}},
                    "required": ["file", "line"]}}},
            {"type": "function", "function": {
                "name": "list_callers",
                "description": "查找某符号在本变更集合中的调用点。用于判断删除/改名的影响范围（C2/N4 类判据取证）。",
                "parameters": {"type": "object", "properties": {
                    "symbol": {"type": "string"}},
                    "required": ["symbol"]}}},
            {"type": "function", "function": {
                "name": "search_repo",
                "description": "在变更文件集合内做大小写不敏感的子串搜索。用于核对同构实现/约定（N1/N2 取证）。",
                "parameters": {"type": "object", "properties": {
                    "pattern": {"type": "string"}},
                    "required": ["pattern"]}}},
        ]

    def call(self, name: str, args: dict) -> str:
        if self.calls_made >= self.max_calls:
            return json.dumps({"error": f"工具预算已用尽（{self.max_calls} 次），请基于已有证据直接给出结论。"},
                              ensure_ascii=False)
        if name not in self.TOOLS:
            return json.dumps({"error": f"未注册工具 {name}（只读白名单外）"}, ensure_ascii=False)
        self.calls_made += 1
        handler = getattr(self, f"_tool_{name}")
        try:
            result = handler(**args)
        except TypeError as e:
            result = {"error": f"参数不合法: {e}"}
        except Exception as e:                      # 工具内部错误不能炸掉审查循环
            result = {"error": f"工具执行失败: {type(e).__name__}: {e}"}
        text = _cap(json.dumps(result, ensure_ascii=False))
        self.trace.append({"tool": name, "args": args,
                           "n": len(result) if isinstance(result, list) else 1})
        return text

    # ---- 实现 ---------------------------------------------------------------

    def _tree(self, path: str) -> ast.Module | None:
        if path not in self._ast_cache:
            try:
                self._ast_cache[path] = ast.parse(self.sources[path])
            except SyntaxError:
                self._ast_cache[path] = None
        return self._ast_cache[path]

    def _tool_find_symbol(self, name: str) -> list:
        hits = []
        for path, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                        and node.name == name:
                    hits.append({"file": path, "line": node.lineno,
                                 "kind": type(node).__name__})
                if len(hits) >= _MAX_RESULTS:
                    return hits
        return hits

    def _tool_get_function_body(self, file: str, line: int) -> dict:
        if file not in self.sources:
            return {"error": f"{file} 不在本次变更集合内（作用域受限）"}
        tree = self._tree(file)
        if tree is None:
            return {"error": f"{file} 解析失败"}
        target: ast.AST | None = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.lineno <= line <= node.end_lineno:
                if target is None or node.lineno >= getattr(target, "lineno", 0):
                    target = node                  # 取最内层
        if target is None:
            return {"error": f"{file}:{line} 不在任何函数内"}
        return {"file": file, "line": target.lineno,
                "source": ast.get_source_segment(self.sources[file], target) or ""}

    def _tool_list_callers(self, symbol: str) -> list:
        hits = []
        for path, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    f = node.func
                    called = f.attr if isinstance(f, ast.Attribute) \
                        else (f.id if isinstance(f, ast.Name) else "")
                    if called == symbol:
                        hits.append({"file": path, "line": node.lineno})
                        if len(hits) >= _MAX_RESULTS:
                            return hits
        return hits

    def _tool_search_repo(self, pattern: str) -> list:
        low = pattern.lower()
        hits = []
        for path in sorted(self.sources):
            for i, line in enumerate(self.sources[path].splitlines(), 1):
                if low in line.lower():
                    hits.append({"file": path, "line": i, "text": line.strip()[:120]})
                    if len(hits) >= _MAX_RESULTS:
                        return hits
        return hits

    def _trees(self):
        for path in sorted(self.sources):
            tree = self._tree(path)
            if tree is not None:
                yield path, tree
