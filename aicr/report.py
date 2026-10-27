"""报告输出：终端可读版 + JSON（与 PR 评论回写共用同一结构）。"""

from __future__ import annotations

import json

from .engine import ReviewResult

_SEV_MARK = {"blocker": "[BLOCKER]", "major": "[major]  ", "minor": "[minor]  "}


def render_terminal(result: ReviewResult) -> str:
    s = result.stats
    lines: list[str] = []
    lines.append(f"aicr 审查报告（判据 {s.criteria_version} · 模式 {s.mode}）")
    lines.append(f"文件 {s.files_reviewed} ｜ 发现 {s.findings} ｜ 预估成本 ¥{s.est_cost_cny:.2f}")
    if s.by_criterion:
        lines.append("按判据：" + "  ".join(f"{k}×{v}" for k, v in sorted(s.by_criterion.items())))
    if s.agent:
        a = s.agent
        if a.get("degraded"):
            lines.append(f"agent：降级（未配置 LLM）｜ 单元 0")
        else:
            lines.append(f"agent：单元 {a.get('units', 0)} ｜ 工具调用 {a.get('tool_calls', 0)}"
                         f" ｜ 迭代 {a.get('iterations', 0)}"
                         + (f" ｜ 失败 {a['errors']}" if a.get("errors") else "")
                         + (f" ｜ 门禁拦截 {a['rejected_by_gate']}" if a.get("rejected_by_gate") else ""))
    lines.append("-" * 72)
    if not result.findings:
        lines.append("（无发现——所有判据未命中或低于置信度阈值）")
    for f in result.findings:
        lines.append(f"{_SEV_MARK.get(f.severity, '[?]')} {f.criterion_id} {f.file}:{f.line}  "
                     f"置信度 {f.confidence:.2f}")
        lines.append(f"        {f.message}")
        if f.evidence:
            lines.append(f"        依据: {'; '.join(f.evidence)}")
        if f.also_flags:
            lines.append(f"        另命中: {', '.join(f.also_flags)}")
    lines.append("-" * 72)
    return "\n".join(lines)


def render_json(result: ReviewResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
