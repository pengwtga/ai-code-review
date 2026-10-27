"""L1 确定性检测器：AST 可判的判据子集，零成本零延迟。

覆盖 M0 判据 10 条 C/E 中的 8 条：C1(lite) C3(lite) C4 C5 E1(lite) E3(lite) E4 E6。
C2 / E2 / E5 需要意图推断 → 归 L2 LLM 层（llm.py，未实测）。

共同约定：
- 只报告「新增行」上的问题（diff_parser.changed_line_numbers）——存量问题不报；
- 置信度锚点写在各检测器内，锚点不足时宁可降置信/不报（判据文档铁律：反例优先）；
- 证据引用 = 文件:行段，全部可回查。
"""

from __future__ import annotations

import ast

from .context import FileContext, annotation_allows_none
from .criteria_schema import Finding

_MUTABLE_CALLOUTS = {"list", "dict", "set"}
_TASK_WRAPPERS = {"create_task", "ensure_future", "gather", "wait",
                  "run", "run_until_complete"}  # asyncio.run / loop.run_until_complete 会消费协程
_GUARD_ZERO_HINTS = {"max", "min"}
_MUTATING_ATTRS = {"append", "extend", "add", "update", "insert", "pop", "remove"}


def _ev(path: str, a: int, b: int | None = None) -> str:
    return f"{path}:{a}" if b is None or b == a else f"{path}:{a}-{b}"


def _ends_with_call(node: ast.expr, suffix: str) -> ast.Call | None:
    """Expr(Call) 且方法名以 suffix 结尾 → 返回 Call。"""
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        f = node.value.func
        name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        if name.endswith(suffix):
            return node.value
    return None


def _call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _receiver_key(call: ast.Call) -> str:
    """acquire/release 的接收者标识：obj 名或方法名。"""
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f.value.id
    return _call_name(call)


def _chain_names(call: ast.Call) -> list[str]:
    """解开链式调用：db.query(...).filter(...).all() → ['all', 'filter', 'query', 'db']。"""
    names: list[str] = []
    cur: ast.expr = call
    while isinstance(cur, ast.Call):
        f = cur.func
        if isinstance(f, ast.Attribute):
            names.append(f.attr)
            cur = f.value
        elif isinstance(f, ast.Name):
            names.append(f.id)
            break
        else:
            break
    return names


# ---------------------------------------------------------------- C1 可空解引用

def detect_c1(fc: FileContext) -> list[Finding]:
    findings: list[Finding] = []
    for func in ast.walk(fc.tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = func.body
        for i, stmt in enumerate(body):
            # 形如 x = <call to optional-returning function>
            if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)):
                continue
            var = stmt.targets[0].id
            call = stmt.value
            if not isinstance(call, ast.Call):
                continue
            callee = None
            for n in _chain_names(call):          # 链式调用：取第一个能解析到模块函数的名字
                callee = fc.function_by_name(n)
                if callee is not None:
                    break
            if callee is None or not annotation_allows_none(callee.returns):
                continue
            guard = _has_null_guard(body[i + 1:], var)
            if guard:
                continue
            use = _first_deref(body[i + 1:], var)
            if use and fc.is_changed(use.lineno):
                findings.append(Finding(
                    criterion_id="C1", file=fc.path, line=use.lineno,
                    severity="blocker", confidence=0.75,
                    message=(f"{var} 可能为 None（{_call_name(call)} 返回注解允许 None，"
                             f"定义于 {fc.path}:{callee.lineno}），此处直接访问可能在空值时崩溃。"
                             f"建议先判空或改用契约保证非空的查询。"),
                    evidence=[_ev(fc.path, stmt.lineno, getattr(callee, 'end_lineno', stmt.lineno)),
                              _ev(fc.path, use.lineno)],
                ))
    return findings


def _has_null_guard(stmts: list[ast.stmt], var: str) -> bool:
    for s in stmts:
        if isinstance(s, ast.If):
            src = ast.unparse(s.test)
            if var in src:
                return True
    return False


def _first_deref(stmts: list[ast.stmt], var: str) -> ast.AST | None:
    """var 的首个属性/下标/方法访问节点。"""
    class V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.hit: ast.AST | None = None
        def visit_Attribute(self, n: ast.Attribute) -> None:
            if self.hit is None and isinstance(n.value, ast.Name) and n.value.id == var:
                self.hit = n
            self.generic_visit(n)
        def visit_Subscript(self, n: ast.Subscript) -> None:
            if self.hit is None and isinstance(n.value, ast.Name) and n.value.id == var:
                self.hit = n
            self.generic_visit(n)
    v = V()
    for s in stmts:
        v.visit(s)
        if v.hit:
            return v.hit
    return None


# ---------------------------------------------------------------- C3 资源未释放

_RESOURCE_CALLS = {"get_conn", "connect", "open_connection", "create_engine", "Session", "acquire_conn"}


def detect_c3(fc: FileContext) -> list[Finding]:
    findings: list[Finding] = []
    for func in ast.walk(fc.tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        src_seg = ast.dump(func)
        for stmt in ast.walk(func):
            if isinstance(stmt, ast.Call) and fc.is_changed(stmt.lineno) \
                    and _call_name(stmt) in _RESOURCE_CALLS \
                    and not _inside_with(stmt, func):
                if not rehas_release(src_seg):
                    findings.append(Finding(
                        criterion_id="C3", file=fc.path, line=stmt.lineno,
                        severity="major", confidence=0.6,
                        message=(f"{_call_name(stmt)} 获取的资源在本函数可见路径上未见 close/release/"
                                 "with 管理，新增路径（尤其早退分支）可能泄漏连接。建议用 with 或 try/finally。"),
                        evidence=[_ev(fc.path, stmt.lineno, getattr(func, 'end_lineno', stmt.lineno))],
                    ))
    return findings


def rehas_release(dump: str) -> bool:
    return any(k in dump for k in ("'close'", "'release'", "'put'", "'disconnect'"))


def _inside_with(node: ast.AST, root: ast.AST) -> bool:
    """判断 node 是否位于某个 With/AsyncWith 的 body 内（v0：靠父链近似）。"""
    for w in ast.walk(root):
        if isinstance(w, (ast.With, ast.AsyncWith)):
            for item in w.items:
                if _subtree_contains(item.context_expr, node):
                    return True
            if _subtree_contains(w, node) and w is not node:
                if any(_subtree_contains(s, node) for s in w.body) and node is not w:
                    pass
    return _parent_in_with(node, root)


def _parent_in_with(node: ast.AST, root: ast.AST) -> bool:
    parents: dict[int, ast.AST] = {}
    for p in ast.walk(root):
        for c in ast.iter_child_nodes(p):
            parents[id(c)] = p
    cur = node
    while id(cur) in parents:
        cur = parents[id(cur)]
        if isinstance(cur, (ast.With, ast.AsyncWith)):
            return True
    return False


def _subtree_contains(root: ast.AST, target: ast.AST) -> bool:
    return any(c is target for c in ast.walk(root))


# ---------------------------------------------------------------- C4 成对操作

def detect_c4(fc: FileContext) -> list[Finding]:
    findings: list[Finding] = []
    for func in ast.walk(fc.tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _finally_releases(func):
            continue  # try-finally 已保证释放，整函数跳过
        stmts = func.body
        for i, stmt in enumerate(stmts):
            acq = _ends_with_call(stmt, "acquire")
            if acq is None:
                continue
            key = _receiver_key(acq)
            for j in range(i + 1, len(stmts)):
                rel = _ends_with_call(stmts[j], "release")
                if rel is not None:
                    break
                bad = _early_exit_in(stmts[j])
                if bad and not _releases_in_subtree(stmts[j]) and fc.is_changed(bad.lineno):
                    findings.append(Finding(
                        criterion_id="C4", file=fc.path, line=bad.lineno,
                        severity="blocker", confidence=0.8,
                        message=(f"在 {key}.acquire() 之后、release() 之前新增了 "
                                 f"{type(bad).__name__} 路径，锁将不会释放。建议用 with / try-finally 包裹。"),
                        evidence=[_ev(fc.path, stmt.lineno), _ev(fc.path, bad.lineno)],
                    ))
                    break
    return findings


def _finally_releases(func: ast.AST) -> bool:
    for n in ast.walk(func):
        if isinstance(n, ast.Try) and n.finalbody:
            for s in n.finalbody:
                for c in ast.walk(s):
                    if isinstance(c, ast.Call) and _call_name(c).endswith("release"):
                        return True
    return False


def _releases_in_subtree(stmt: ast.stmt) -> bool:
    """早退分支内自带 release（先释放再 return 是合规写法，反例形态）。"""
    return any(isinstance(c, ast.Call) and _call_name(c).endswith("release")
               for c in ast.walk(stmt))


def _early_exit_in(stmt: ast.stmt) -> ast.stmt | None:
    for n in ast.walk(stmt):
        if isinstance(n, (ast.Return, ast.Raise)) and not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return n
    return None


# ---------------------------------------------------------------- C5 可变默认参数

def detect_c5(fc: FileContext) -> list[Finding]:
    findings: list[Finding] = []
    for func in ast.walk(fc.tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        mutables = [d for d in list(func.args.defaults) + [k for k in func.args.kw_defaults if k]
                    if isinstance(d, (ast.List, ast.Dict, ast.Set,
                                      ast.ListComp, ast.DictComp, ast.SetComp))
                    or (isinstance(d, ast.Call) and _call_name(d) in _MUTABLE_CALLOUTS)]
        if not mutables:
            continue
        # 置信度锚点：函数体内是否真的修改了该参数
        mutated = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in _MUTATING_ATTRS
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id in {a.arg for a in func.args.args}
            for n in ast.walk(func))
        if not (fc.is_changed(func.lineno) or any(fc.is_changed(m.lineno) for m in mutables)):
            continue
        findings.append(Finding(
            criterion_id="C5", file=fc.path, line=mutables[0].lineno,
            severity="major", confidence=0.9 if mutated else 0.7,
            message=("函数签名使用了可变默认值（list/dict/set），默认对象会在多次调用间共享并被修改，"
                     "造成跨调用状态残留。建议默认 None 并在函数体内新建。"),
            evidence=[_ev(fc.path, func.lineno, getattr(func, 'end_lineno', func.lineno))],
        ))
    return findings


# ---------------------------------------------------------------- E1 空容器访问

_QUERY_HINTS = ("filter", "query", "find", "select", "match", "search", "fetch", "lookup")


def detect_e1(fc: FileContext) -> list[Finding]:
    findings: list[Finding] = []
    for func in ast.walk(fc.tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = func.body
        for i, stmt in enumerate(body):
            if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)):
                continue
            var = stmt.targets[0].id
            call = stmt.value
            if not isinstance(call, ast.Call):
                continue
            names = _chain_names(call)
            matched = next((n for n in names
                            if any(h in n.lower() for h in _QUERY_HINTS)), None)
            if matched is None:
                continue
            if _has_empty_guard(body[i + 1:], var):
                continue
            access = _first_empty_access(body[i + 1:], var)
            if access and fc.is_changed(access.lineno):
                findings.append(Finding(
                    criterion_id="E1", file=fc.path, line=access.lineno,
                    severity="major", confidence=0.6,
                    message=(f"{var} 来自查询/过滤调用（{matched}），可能为空，此处取首元素/"
                             "极值会在空集合上抛错。建议先判空或提供默认值。"),
                    evidence=[_ev(fc.path, stmt.lineno), _ev(fc.path, access.lineno)],
                ))
    return findings


def _has_empty_guard(stmts: list[ast.stmt], var: str) -> bool:
    for s in stmts:
        if isinstance(s, ast.If):
            src = ast.unparse(s.test)
            if var in src and any(k in src for k in ("not ", "len(", "== 0", "!= 0", " if ")):
                return True
    return False


def _first_empty_access(stmts: list[ast.stmt], var: str) -> ast.AST | None:
    class V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.hit: ast.AST | None = None
        def visit_Subscript(self, n: ast.Subscript) -> None:
            if self.hit is None and isinstance(n.value, ast.Name) and n.value.id == var \
                    and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, int):
                self.hit = n
            self.generic_visit(n)
        def visit_Call(self, n: ast.Call) -> None:
            if self.hit is None and _call_name(n) in ("next", "max", "min") \
                    and any(isinstance(a, ast.Name) and a.id == var for a in n.args):
                self.hit = n
            self.generic_visit(n)
    v = V()
    for s in stmts:
        v.visit(s)
        if v.hit:
            return v.hit
    return None


# ---------------------------------------------------------------- E3 除零/模零

def _den_key(den: ast.expr) -> str | None:
    """分母标识：Name 用变量名，Attribute 用 a.b 表达式文本（真实代码两种都常见）。

    限制：只接受 ≤3 段的属性链，且不接受调用（a.count() 这类动态结果不判，
    避免把 len()/count() 的返回值误当可判分母）。
    """
    if isinstance(den, ast.Name):
        return den.id
    if isinstance(den, ast.Attribute):
        src = ast.unparse(den)
        if src.count(".") <= 2 and "(" not in src:
            return src
    return None


def detect_e3(fc: FileContext) -> list[Finding]:
    findings: list[Finding] = []
    for func in ast.walk(fc.tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = func.body
        parents: dict[int, ast.AST] = {}
        for p in ast.walk(func):
            for c in ast.iter_child_nodes(p):
                parents[id(c)] = p
        for node in ast.walk(func):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Mod))):
                continue
            if not fc.is_changed(node.lineno):
                continue
            key = _den_key(node.right)
            if key is None:
                continue
            if _has_zero_guard(body, key, node.lineno):
                continue
            if _inside_ifexp_guard(node, parents, key):
                continue  # 行内三元守卫 x/total if total else 0
            findings.append(Finding(
                criterion_id="E3", file=fc.path, line=node.lineno,
                severity="major", confidence=0.6,
                message=(f"除数 {key} 来自计算或参数，可见范围内无零值防护，空集合聚合（如 sum()）"
                         "或入参为 0 时会抛 ZeroDivisionError。建议先判零或用 max(den, 1) 语义确认。"),
                evidence=[_ev(fc.path, node.lineno, getattr(func, 'end_lineno', node.lineno))],
            ))
    return findings


def _inside_ifexp_guard(node: ast.AST, parents: dict[int, ast.AST], name: str) -> bool:
    """除法节点是否处于以 name 为条件的 IfExp 保护分支内（如 round(x/total, 4) if total else 0）。"""
    cur = node
    while id(cur) in parents:
        cur = parents[id(cur)]
        if isinstance(cur, ast.IfExp) and name in ast.unparse(cur.test):
            return True
    return False


def _has_zero_guard(stmts: list[ast.stmt], name: str, before_line: int) -> bool:
    for s in stmts:
        for n in ast.walk(s):
            if isinstance(n, ast.Call) and _call_name(n) in _GUARD_ZERO_HINTS \
                    and any(_den_key(a) == name for a in n.args):
                return True
            if isinstance(s, ast.If):
                src = ast.unparse(s.test)
                if name in src:
                    return True
        if s.lineno and s.lineno > before_line:
            break
    return False


# ---------------------------------------------------------------- E4 异常静默吞掉

def detect_e4(fc: FileContext) -> list[Finding]:
    findings: list[Finding] = []
    for node in ast.walk(fc.tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not fc.is_changed(node.lineno):
            continue
        body_all_pass = all(isinstance(s, ast.Pass) for s in node.body)
        if not body_all_pass:
            continue
        broad = node.type is None or (isinstance(node.type, ast.Name)
                                      and node.type.id in ("Exception", "BaseException"))
        if not broad:
            continue
        findings.append(Finding(
            criterion_id="E4", file=fc.path, line=node.lineno,
            severity="major", confidence=0.9,
            message=("except 静默吞掉异常（pass），失败不可见且难排查。建议至少记录日志；"
                     "若确属可忽略的清理路径，请收窄异常类型并注明 best-effort。"),
            evidence=[_ev(fc.path, node.lineno, getattr(node, 'end_lineno', node.lineno))],
        ))
    return findings


# ---------------------------------------------------------------- E6 协程未 await

def detect_e6(fc: FileContext) -> list[Finding]:
    async_names = {n.name for n in ast.walk(fc.tree)
                   if isinstance(n, ast.AsyncFunctionDef)}
    if not async_names:
        return []
    findings: list[Finding] = []
    parents: dict[int, ast.AST] = {}
    for p in ast.walk(fc.tree):
        for c in ast.iter_child_nodes(p):
            parents[id(c)] = p

    for node in ast.walk(fc.tree):
        if not (isinstance(node, ast.Call) and fc.is_changed(node.lineno)):
            continue
        fname = _call_name(node)
        if fname not in async_names:
            continue
        # 沿父链检查是否已被 await / 任务包装
        cur, ok = node, False
        chain = []
        while id(cur) in parents:
            cur = parents[id(cur)]
            chain.append(cur)
            if isinstance(cur, ast.Await):
                ok = True
                break
            if isinstance(cur, ast.Call) and _call_name(cur) in _TASK_WRAPPERS:
                ok = True
                break
            if len(chain) > 3:
                break
        if ok:
            continue
        findings.append(Finding(
            criterion_id="E6", file=fc.path, line=node.lineno,
            severity="blocker", confidence=0.85,
            message=(f"在协程上下文中调用了 async 函数 {fname}() 但未 await，"
                     "返回的是 coroutine 对象而非结果，静默产生错误数据。建议补 await"
                     "（如确需并发任务请用 asyncio.create_task 并保留引用）。"),
            evidence=[_ev(fc.path, node.lineno)],
        ))
    return findings


def detect_m4(fc: FileContext) -> list[Finding]:
    """M4 不可达与恒假分支（仅新增）：新增行上的 if False / while False 等恒假测试。"""
    findings: list[Finding] = []
    for node in ast.walk(fc.tree):
        if not isinstance(node, (ast.If, ast.While)):
            continue
        test = node.test
        if isinstance(test, ast.Constant) and not test.value:
            if fc.is_changed(node.lineno):      # M0 口径：仅报告新增的不可达分支
                findings.append(Finding(
                    criterion_id="M4", file=fc.path, line=node.lineno,
                    severity="major", confidence=0.8,
                    message=f"恒假条件（{ast.unparse(test)}）导致该分支永不执行——"
                            "若是调试残留请删除；若意图是禁用逻辑请显式注释并配置化。",
                    evidence=[_ev(fc.path, node.lineno, getattr(node, 'end_lineno', node.lineno))],
                ))
    return findings


DETERMINISTIC_DETECTORS = [detect_c1, detect_c3, detect_c4, detect_c5,
                           detect_e1, detect_e3, detect_e4, detect_e6, detect_m4]
