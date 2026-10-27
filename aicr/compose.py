"""评论装配（M2）：ReviewResult → GitHub PR review 的行级评论载荷。

- 行锚定规则：inline 评论必须锚定在 diff 的新侧行号上（side=RIGHT）。
  finding 按构造只报变更行，但仍以 diff 实际行集合做二次校验——
  不在锚定集合内的 finding 降级进 summary 正文（防 422 API 错误）。
- summary 是唯一兜底出口：无行级发现时也发（"审查完成，无发现"），
  让团队知道引擎跑过——沉默会造成"部署了没生效"的误解。
"""

from __future__ import annotations

from .diff_parser import parse_unified_diff
from .engine import ReviewResult

_MARK = {"blocker": "🔴 BLOCKER", "major": "🟠 major", "minor": "🟡 minor"}
FOOTER = "<!-- aicr-bot:v1 判据见 M0 白名单，本评论由 review-engine 生成 -->"


def anchorable_lines(diff_text: str) -> dict[str, set[int]]:
    """从 diff 提取可锚定集合：{path: {新侧行号}}（上下文行 + 新增行）。"""
    out: dict[str, set[int]] = {}
    for fd in parse_unified_diff(diff_text):
        path = fd.new_path or fd.old_path
        lines = out.setdefault(path, set())
        for hunk in fd.hunks:
            for l in hunk.lines:
                if l.kind in (" ", "+") and l.new_lineno is not None:
                    lines.add(l.new_lineno)
    return out


def compose(result: ReviewResult, diff_text: str,
            repo_label: str = "") -> tuple[str, list[dict], list]:
    """返回 (summary_body, inline_comments, skipped_findings)。

    inline_comments 元素形如 {"path", "side", "line", "body"}，可直接发给
    POST /pulls/{n}/reviews。skipped 是锚不上的 finding（并入 summary）。
    """
    anchors = anchorable_lines(diff_text)
    comments: list[dict] = []
    skipped: list = []
    for f in result.findings:
        if f.line in anchors.get(f.file, set()):
            comments.append({
                "path": f.file,
                "side": "RIGHT",
                "line": f.line,
                "body": _inline_body(f),
            })
        else:
            skipped.append(f)
    return _summary_body(result, skipped, repo_label), comments, skipped


def _inline_body(f) -> str:
    parts = [f"**{_MARK.get(f.severity, '?')} {f.criterion_id}** 置信度 {f.confidence:.2f}",
             f.message]
    if f.evidence:
        parts.append("依据: " + "; ".join(f.evidence))
    if f.also_flags:
        parts.append(f"另命中: {', '.join(f.also_flags)}")
    return "\n\n".join(parts) + "\n\n" + FOOTER


def _summary_body(result: ReviewResult, skipped: list, repo_label: str) -> str:
    s = result.stats
    head = f"aicr 审查报告（判据 {s.criteria_version} · 模式 {s.mode}）"
    if repo_label:
        head += f" · {repo_label}"
    lines = [head,
             f"文件 {s.files_reviewed} ｜ 发现 {s.findings}"
             f" ｜ 预估成本 ¥{s.est_cost_cny:.2f}",
             f"平均发现密度 {s.findings / s.files_reviewed:.2f} 条/文件"]
    if s.by_criterion:
        lines.append("按判据：" + "  ".join(f"{k}×{v}" for k, v in sorted(s.by_criterion.items())))
    if s.agent:
        a = s.agent
        if a.get("degraded"):
            lines.append("agent：降级（未配置 LLM）")
        else:
            lines.append(f"agent：单元 {a.get('units', 0)} ｜ 工具调用 {a.get('tool_calls', 0)}"
                         + (f" ｜ 门禁拦截 {a['rejected_by_gate']}"
                            if a.get("rejected_by_gate") else ""))
    if skipped:
        lines.append("\n**未锚定到 diff 行的发现**（已并入本条，不发行级评论）：")
        for f in skipped:
            lines.append(f"- `{f.file}:{f.line}` {f.criterion_id} {f.message}")
    if not result.findings:
        lines.append("（无发现——所有判据未命中或低于置信度阈值）")
    lines.append("\n> 引擎为非阻断模式：不 approve、不 block，评论仅供参考。")
    return "\n".join(lines) + "\n" + FOOTER
