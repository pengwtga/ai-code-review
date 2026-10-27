"""L2 LLM 判定层（接口就绪，未实测）。

职责：把确定性层覆盖不了的判据（C2 / E2 / E5，以及全部 N / M 路）交给 LLM，
判据白名单直接注入 prompt（M0 第 2 节铁律 1：判据即白名单，模型只许报清单内问题）。
OpenAI 兼容协议（DeepSeek / 智谱 / SiliconFlow 均适用），provider 可整体替换。

状态：代码就绪、未接 key 实测——engine.enable_llm=False 时不走此路径。
"""

from __future__ import annotations

import json
import os
from .criteria_schema import CRITERIA, Finding

_L2_CRITERIA = ["C2", "E2", "E5"]  # M1 范围内需要 LLM 的判据；N/M 路在 M1.5/M2 接入

_SYSTEM = "你是代码审查引擎的判定器。只允许报告白名单判据内的问题，输出 JSON。"

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
    """OpenAI 兼容 chat.completions 调用。未配置时抛 RuntimeError，由调用方降级。"""
    api_base = os.environ.get("AICR_API_BASE")
    api_key = os.environ.get("AICR_API_KEY")
    model = os.environ.get("AICR_MODEL", "deepseek-chat")
    if not api_base or not api_key:
        raise RuntimeError("AICR_API_BASE / AICR_API_KEY 未配置，L2 层降级为仅确定性检测")

    import urllib.request

    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": _SYSTEM},
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
