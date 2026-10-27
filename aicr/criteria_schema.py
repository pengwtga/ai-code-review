"""Finding 数据结构：对齐 M0 判据文档第 3 节输出契约。

铁律 3：每条 finding 必须引用 criterion_id；evidence 为空则不渲染。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2}

# M0 判据编号 → 一句话说明（白名单注册表；LLM 路的 prompt 也从这里生成）
CRITERIA: dict[str, str] = {
    "C1": "可空解引用：接收者无法证明非空且直接访问属性/下标",
    "C2": "复制粘贴变量错位：同模板表达式引用了错误变量",
    "C3": "资源未释放：新增获取的资源在所有退出路径无释放",
    "C4": "提前返回破坏成对操作：acquire/release 之间新增 return/raise",
    "C5": "可变默认参数：函数签名使用可变默认值且体内修改",
    "E1": "空容器访问：可能为空的序列做首元素访问且无防护",
    "E2": "分页/索引 off-by-one：偏移计算与计数语义不符",
    "E3": "除零与模零：分母无零值防护",
    "E4": "异常静默吞掉：except pass，不记录不重抛",
    "E5": "异常捕获过宽：非任务边界处 except Exception 且不重抛",
    "E6": "协程未 await：async 内调用协程未等待",
    "N1": "同 PR 同构改动不一致：同类操作中唯一缺失某个防护步骤",
    "N2": "同模块重造既有实现",
    "N3": "成对资源获取顺序不一致（死锁向）",
    "N4": "变更不完整：定义改了同 PR 引用点未同步",
    "M1": "静默放宽既有约束：删除校验/断言且无补偿",
    "M2": "复制漂移：与既有实现 >10 行重复且小改",
    "M3": "隐式时序/状态依赖",
    "M4": "不可达与恒假分支（仅新增）",
}


@dataclass
class Finding:
    criterion_id: str
    file: str
    line: int
    severity: str                      # blocker | major | minor
    confidence: float                  # 0-1
    message: str
    evidence: list[str] = field(default_factory=list)   # 形如 "path:12-18" 的上下文引用
    also_flags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.criterion_id not in CRITERIA:
            raise ValueError(f"未注册的判据 ID: {self.criterion_id}（白名单外输出非法）")
        if not self.evidence:
            raise ValueError("finding 缺少 evidence：无引用不渲染（M0 输出契约）")

    def to_dict(self) -> dict:
        return asdict(self)

    def sort_key(self) -> tuple:
        return (SEVERITY_ORDER.get(self.severity, 9), -self.confidence, self.file, self.line)


@dataclass
class ReviewStats:
    files_reviewed: int = 0
    criteria_run: int = 0
    findings: int = 0
    by_criterion: dict = field(default_factory=dict)
    mode: str = "deterministic"        # deterministic | hybrid
    criteria_version: str = "v0.1"
    est_cost_cny: float = 0.0          # 确定性层成本为 0；LLM 层由 cost.py 记账
    agent: dict = field(default_factory=dict)   # L2 agent 统计：units/tool_calls/iterations/errors
