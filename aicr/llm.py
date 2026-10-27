"""L2 agent 判定层（M1.5：工具循环版）。

架构立场（对应竞品文档的差异化叙事）：
Anthropic 验证式 = 自由 agent（每条结论回代码核对，20 分钟 / $15-25）；
本引擎 = **预算受限的受控 agent**——模型可在只读工具白名单内自主取证，
但工具调用次数、作用域（仅变更文件）、迭代轮数全部有硬上限，全程 trace 可回放。

降级链：函数调用不可用 → 退回单轮 JSON prompt（M1 路径）；无 key → 确定性层 only。
OpenAI 兼容协议（DeepSeek / 智谱 / SiliconFlow 均适用）。
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

from .criteria_schema import CRITERIA, Finding
from .tools import ReviewToolbox


def load_dotenv() -> None:
    """加载 CWD 或项目根的 .env（不覆盖已设置的环境变量）。本地 key 不入库。"""
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
        break


load_dotenv()

_L2_CRITERIA = ["C2", "E2", "E5"]      # M1.5 范围内需要 LLM 的判据；N/M 路 M1.5 后期/M2 接入

_MAX_ITERATIONS = 4

_AGENT_SYSTEM = """你是代码审查引擎的取证判定器。规则：
1. 只允许报告白名单判据内的问题；白名单外的问题即使存在也忽略（判据即白名单）。
2. 你可以在只读工具白名单内自主取证（查符号、看函数体、找调用点、搜索），
   但工具预算有限（每单元最多 {max_calls} 次），先判断是否真需要取证再调用。
3. 每条结论必须给出证据：行号引用，或工具调用发现的 file:line。
4. 不确定就不报——漏报可归因，误报直接摧毁工具可信度。
5. 证据足够后立即输出结论，不要为凑轮次调用工具。
最终输出 JSON 数组（不要包裹其他文字），每项：
{{"criterion_id","line","severity":"blocker|major","confidence":0-1,"message","evidence_refs":["file:line" 或 "tool:工具名:发现简述"]}}
无命中输出 []。"""

# ---- 工具循环 ----------------------------------------------------------------


def agent_review_unit(path: str, function_source: str, func_start_line: int,
                      changed: list[int], toolbox: ReviewToolbox,
                      transport=None) -> tuple[list[Finding], dict]:
    """对一个变更单元（函数）跑受控 agent 循环，返回 (findings, trace)。"""
    transport = transport or _http_transport
    criteria_text = "\n".join(f"- {cid}: {desc}"
                              for cid, desc in CRITERIA.items() if cid in _L2_CRITERIA)
    sys_prompt = _AGENT_SYSTEM.format(max_calls=toolbox.max_calls)
    user_prompt = (f"变更文件：{path}；变更行（绝对行号）：{changed}\n"
                   f"当前函数起始行：{func_start_line}（行号请用文件绝对行号）。\n\n"
                   f"可用判据白名单：\n{criteria_text}\n\n"
                   f"函数全文：\n```python\n{function_source}\n```")

    messages: list[dict] = [{"role": "system", "content": sys_prompt},
                            {"role": "user", "content": user_prompt}]
    trace: dict = {"iterations": 0, "tool_calls": 0, "tools_used": {}, "degraded": False}

    for _ in range(_MAX_ITERATIONS):
        trace["iterations"] += 1
        resp = transport(messages, toolbox.spec)
        msg = resp.get("choices", [{}])[0].get("message", {})
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return _collect(msg.get("content") or "", path, trace,
                            function_source, func_start_line)
        messages.append(msg)
        for tc in tool_calls:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = toolbox.call(fn.get("name", ""), args)
            trace["tool_calls"] += 1
            tname = fn.get("name", "?")
            trace["tools_used"][tname] = trace["tools_used"].get(tname, 0) + 1
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": result})

    # 迭代上限：强制无工具收敛
    messages.append({"role": "user",
                     "content": "工具预算与轮次已用尽。请立即基于已有证据输出最终 JSON 结论（可为 []）。"})
    resp = transport(messages, None)
    msg = resp.get("choices", [{}])[0].get("message", {})
    return _collect(msg.get("content") or "", path, trace,
                    function_source, func_start_line)


# ---- 证据门禁：LLM 声称的判据必须通过确定性语法核验 ----------------------------
# 背景（真实样本教训）：glm-4-flash 在无 except 的函数上凭空报告 E5×6（置信度 1.0）。
# 铁律：LLM 的声称是「假设」，判据的语法前提必须能在代码里被确定性证实。


def _gate_e5(function_source: str) -> bool:
    """E5 前提：函数内存在 except Exception（裸捕获）。"""
    try:
        tree = ast.parse(function_source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            t = node.type
            names = [t] if isinstance(t, ast.Name) else (t.elts if isinstance(t, ast.Tuple) else [])
            if any(isinstance(n, ast.Name) and n.id == "Exception" for n in names):
                return True
    return False


def _gate_e2(function_source: str) -> bool:
    """E2 前提：函数内存在下标切片或 range() 计算。"""
    try:
        tree = ast.parse(function_source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            return True
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "range":
                return True
    return False


def _gate_c2(function_source: str, line: int, func_start_line: int) -> bool:
    """C2（复制粘贴变量错位）：正确样板可能在其他文件，无法函数内强验。
    弱门禁：报告行必须是真实代码（非空、非注释）。"""
    try:
        text = function_source.splitlines()[line - func_start_line].strip()
    except IndexError:
        return False
    return bool(text) and not text.startswith("#")


_GATES = {"E5": _gate_e5, "E2": _gate_e2, "C2": _gate_c2}


def verify_finding(cid: str, function_source: str, func_start_line: int,
                   line: int) -> bool:
    """证据门禁：行号必须在函数范围内，且判据的语法前提成立。"""
    n_lines = function_source.count("\n") + 1
    if not (func_start_line <= line < func_start_line + n_lines):
        return False
    gate = _GATES.get(cid)
    if gate is None:
        return True
    if gate.__code__.co_argcount == 3:          # (source, line, start) 弱门禁
        return gate(function_source, line, func_start_line)
    return gate(function_source)


def _collect(content: str, path: str, trace: dict,
             function_source: str = "", func_start_line: int = 1) -> tuple[list[Finding], dict]:
    findings: list[Finding] = []
    rejected = 0
    for item in _parse_lenient(content):
        cid = item.get("criterion_id")
        if cid not in _L2_CRITERIA:            # 白名单外直接丢弃（比全 CRITERIA 更严：L2 只管自己的路）
            continue
        evidence = [str(r) for r in item.get("evidence_refs", [])]
        if not evidence:
            continue                            # 无引用不渲染（铁律 3）
        try:
            f = Finding(
                criterion_id=cid, file=item.get("file", path),
                line=int(item.get("line", 0)),
                severity=item.get("severity", "major"),
                confidence=float(item.get("confidence", 0)),
                message=item.get("message", ""), evidence=evidence)
        except (TypeError, ValueError):
            continue
        if not verify_finding(cid, function_source, func_start_line, f.line):
            rejected += 1                       # 幻觉拦截：语法前提不成立
            continue
        findings.append(f)
    if rejected:
        trace["rejected_by_gate"] = trace.get("rejected_by_gate", 0) + rejected
    return findings, trace


# ---- 传输层（OpenAI 兼容） ----------------------------------------------------


def _http_transport(messages: list[dict], tools: list[dict] | None) -> dict:
    api_base = os.environ.get("AICR_API_BASE")
    api_key = os.environ.get("AICR_API_KEY")
    model = os.environ.get("AICR_MODEL", "deepseek-chat")
    if not api_base or not api_key:
        raise RuntimeError("AICR_API_BASE / AICR_API_KEY 未配置，L2 层降级为仅确定性检测")
    body: dict = {"model": model, "messages": messages, "temperature": 0}
    if tools:
        body["tools"] = tools
    import urllib.request
    req = urllib.request.Request(
        f"{api_base.rstrip('/')}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


# ---- 降级路径：单轮 JSON（无函数调用能力的模型 / M1 行为保持） ------------------

_PROMPT = """审查以下 Python 变更。变更行（新文件行号）：{changed}

可用判据白名单（只允许报告这些）：
{criteria}

变更单元所在函数（全文）：
```python
{function_source}
```

对每条判据独立判定：是否命中、置信度（0-1）、依据（引用函数内行号）。不确定就不报。
输出 JSON 数组，每项：{{"criterion_id","line","severity":"blocker|major","confidence","message","evidence_refs":[]}}。无命中输出 []。
"""


def build_prompt(function_source: str, changed: list[int]) -> str:
    criteria_text = "\n".join(f"- {cid}: {desc}" for cid, desc in CRITERIA.items() if cid in _L2_CRITERIA)
    return _PROMPT.format(changed=changed, criteria=criteria_text, function_source=function_source)


def call_llm(prompt: str) -> list[Finding]:
    """单轮调用（降级路径）。未配置时抛 RuntimeError，由调用方降级。"""
    api_base = os.environ.get("AICR_API_BASE")
    api_key = os.environ.get("AICR_API_KEY")
    model = os.environ.get("AICR_MODEL", "deepseek-chat")
    if not api_base or not api_key:
        raise RuntimeError("AICR_API_BASE / AICR_API_KEY 未配置，L2 层降级为仅确定性检测")

    import urllib.request

    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": "你是代码审查引擎的判定器。只允许报告白名单判据内的问题，输出 JSON。"},
                     {"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        f"{api_base.rstrip('/')}/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    content = data["choices"][0]["message"]["content"]

    findings: list[Finding] = []
    for item in _parse_lenient(content):
        cid = item.get("criterion_id")
        if cid not in CRITERIA:            # 白名单外输出直接丢弃（铁律 1）
            continue
        findings.append(Finding(
            criterion_id=cid, file=item.get("file", ""), line=int(item.get("line", 0)),
            severity=item.get("severity", "major"),
            confidence=float(item.get("confidence", 0)),
            message=item.get("message", ""),
            evidence=[str(r) for r in item.get("evidence_refs", [])] or ["llm:无行级引用"],
        ))
    return findings


def _parse_lenient(content: str) -> list:
    """glm-4-flash 等不支持 JSON mode 时的宽松提取（沿用 ssrm-aiops 的降级经验）。"""
    if not content or not isinstance(content, str):
        return []
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("["), content.rfind("]")
        if start == -1 or end == -1:
            return []
        try:
            data = json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else data.get("findings", [])
