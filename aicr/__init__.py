"""aicr — AI Code Review engine.

M1 骨架：diff 解析 → 变更单元 → 确定性判据检测（AST）→ 聚合去重 → 结构化输出。
LLM 二级判定（L2）接口已留，待接 provider 后实测。
判据白名单与编号对齐《M0-四路审查判据》v0.1。
"""

__version__ = "0.1.0"
CRITERIA_VERSION = "v0.1"
