"""实验：L2 agent 循环次数扫描（迭代上限 ∈ {1, 2, 4}）。

问题：_MAX_ITERATIONS=4 / max_calls=8 是设计常数，不是测量值。
本实验在固定单元集合上扫迭代上限，观测：
  iterations_used（实际用几轮收敛）/ tool_calls / findings / gate 拦截 / 耗时。
结论写回 README「真模型实测记录」。样本小，只做方向性判断，不做强声明。

用法：python experiments/iter_sweep.py --repo ../ssrm-aiops --base <empty-tree> --head 0378b5a
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicr import llm as llm_mod                     # noqa: E402
from aicr.context import FileContext                # noqa: E402
from aicr.diff_parser import changed_line_numbers   # noqa: E402
from aicr.tools import ReviewToolbox                # noqa: E402


def extract_units(repo: str, base: str, head: str, cap: int):
    """复刻 engine 的 L2 单元切割：变更行 → enclosing function。"""
    from aicr.cli import _load_git_pair
    from aicr.diff_parser import make_unified_diff, parse_unified_diff
    old, new = _load_git_pair(Path(repo), base, head)
    fd_list = parse_unified_diff(make_unified_diff(old, new))
    units = []
    for fd in fd_list:
        path = fd.new_path or fd.old_path
        if not path.endswith(".py"):
            continue
        fc = FileContext(path=path, new_source=new.get(path, ""),
                         changed_lines=changed_line_numbers(fd))
        by_scope: dict[str, list[int]] = {}
        for ln in sorted(fc.changed_lines):
            func = fc.function_at(ln)
            key = func.name if func is not None else "<module>"
            by_scope.setdefault(key, []).append(ln)
        for key, lines in by_scope.items():
            if key == "<module>":
                continue
            func = fc.function_by_name(key)
            units.append((path, fc.function_source(func), func.lineno, lines))
    # 优先保留含 except 的单元（正例），其余截断
    units.sort(key=lambda u: 0 if "except" in u[1] else 1)
    return units[:cap]


def sweep(units, max_iter: int) -> dict:
    llm_mod._MAX_ITERATIONS = max_iter          # 运行时覆盖，机制同生产路径
    t0 = time.time()
    agg = {"findings": 0, "tool_calls": 0, "iter_used": 0, "gated": 0,
           "by_criterion": {}, "units_concluded_1st": 0}
    for path, func_src, lineno, lines in units:
        tb = ReviewToolbox(sources={path: func_src}, max_calls=8)
        fs, trace = llm_mod.agent_review_unit(path, func_src, lineno, lines, tb)
        agg["findings"] += len(fs)
        agg["tool_calls"] += trace["tool_calls"]
        agg["iter_used"] += trace["iterations"]
        agg["gated"] += trace.get("rejected_by_gate", 0)
        agg["units_concluded_1st"] += 1 if trace["iterations"] == 1 else 0
        for f in fs:
            agg["by_criterion"][f.criterion_id] = agg["by_criterion"].get(f.criterion_id, 0) + 1
    agg["elapsed_s"] = round(time.time() - t0, 1)
    return agg


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--cap", type=int, default=12, help="单元数上限（控成本）")
    args = ap.parse_args()

    units = extract_units(args.repo, args.base, args.head, args.cap)
    print(f"单元数 {len(units)}（含 except 的优先）\n")
    print(f"{'上限':>4} | {'发现':>4} | {'门禁拦截':>6} | {'工具调用':>6} | "
          f"{'迭代总数':>6} | {'1轮收敛':>6} | 耗时")
    print("-" * 72)
    results = {}
    for k in (1, 2, 4):
        r = sweep(units, k)
        results[k] = r
        print(f"{k:>4} | {r['findings']:>4} | {r['gated']:>6} | {r['tool_calls']:>6} | "
              f"{r['iter_used']:>6} | {r['units_concluded_1st']:>6} | {r['elapsed_s']}s"
              f"   判据: {r['by_criterion']}")
    Path("experiments/iter_sweep_result.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
