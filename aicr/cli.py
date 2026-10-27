"""CLI 入口。

    aicr review --repo PATH --base REV [--head REV]   # git 模式（M1 主路径）
    aicr review --diff FILE [--repo PATH]             # 现成 diff（webhook 同构路径）
    aicr eval  [--cases evals/cases.json]             # 注入回归集
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .engine import ReviewEngine
from .report import render_terminal, render_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aicr", description="AI Code Review engine (M1)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rev = sub.add_parser("review", help="审查一次变更")
    p_rev.add_argument("--repo", type=Path, default=None, help="仓库路径（diff-only 模式下用于读取新文件全文）")
    p_rev.add_argument("--base", default=None, help="基准 revision（git 模式）")
    p_rev.add_argument("--head", default=None, help="目标 revision（git 模式，默认工作区/HEAD）")
    p_rev.add_argument("--diff", type=Path, default=None, help="现成 unified diff 文件")
    p_rev.add_argument("--json-out", type=Path, default=None, help="结果 JSON 输出路径")
    p_rev.add_argument("--threshold", type=float, default=0.55, help="置信度阈值（低于不渲染）")
    p_rev.add_argument("--llm", action="store_true",
                       help="启用 L2 agent 层（只读工具 + 预算受控；需 AICR_API_BASE/AICR_API_KEY）")

    p_eval = sub.add_parser("eval", help="运行注入回归集")
    p_eval.add_argument("--cases", type=Path, default=Path("evals/cases.json"))
    p_eval.add_argument("--fixtures", type=Path, default=Path("fixtures/cases"))

    args = parser.parse_args(argv)

    if args.cmd == "review":
        return _cmd_review(args)
    if args.cmd == "eval":
        return _cmd_eval(args)
    return 2


# --------------------------------------------------------------------------- review

def _cmd_review(args) -> int:
    engine = ReviewEngine(confidence_threshold=args.threshold, enable_llm=args.llm)
    if args.diff:
        diff_text = args.diff.read_text(encoding="utf-8", errors="replace")
        result = engine.review_diff(diff_text, repo=args.repo)
    elif args.repo and args.base:
        old, new = _load_git_pair(args.repo, args.base, args.head)
        result = engine.review(old, new)
    else:
        print("需要 --diff 或（--repo + --base）", file=sys.stderr)
        return 2

    print(render_terminal(result))
    if args.json_out:
        args.json_out.write_text(render_json(result), encoding="utf-8")
    return 1 if result.findings else 0


def _load_git_pair(repo: Path, base: str, head: str | None) -> tuple[dict, dict]:
    def git(*cmd: str) -> str:
        r = subprocess.run(["git", "-C", str(repo), *cmd], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(cmd)} 失败: {r.stderr.strip()}")
        return r.stdout

    rev_pair = f"{base}..{head}" if head else base
    names = git("diff", "--name-only", rev_pair).splitlines()
    old: dict[str, str] = {}
    new: dict[str, str] = {}
    for name in names:
        if not name.endswith(".py"):
            continue
        try:
            old[name] = git("show", f"{base}:{name}")
        except RuntimeError:
            old[name] = ""                       # 新增文件
        head_ref = head if head else ""
        try:
            new[name] = git("show", f"{head_ref}:{name}") if head_ref else \
                (repo / name).read_text(encoding="utf-8", errors="replace")
        except (RuntimeError, OSError):
            new[name] = ""                       # 删除文件
    return old, new


# --------------------------------------------------------------------------- eval

def _cmd_eval(args) -> int:
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    engine = ReviewEngine()
    passed = failed = 0
    rows: list[str] = []
    for case in cases["cases"]:
        fdir = args.fixtures / case["id"]
        if "files" in case:                      # 多文件夹具（N1 等 PR 级判据）
            old = {p: (fdir / spec["before"]).read_text(encoding="utf-8")
                   for p, spec in case["files"].items()}
            new = {p: (fdir / spec["after"]).read_text(encoding="utf-8")
                   for p, spec in case["files"].items()}
        else:
            old = {case["path"]: (fdir / case["before"]).read_text(encoding="utf-8")}
            new = {case["path"]: (fdir / case["after"]).read_text(encoding="utf-8")}
        result = engine.review(old, new)
        got: dict[str, int] = {}
        for f in result.findings:
            got[f.criterion_id] = got.get(f.criterion_id, 0) + 1
        ok = _check_expect(case["expect"], got)
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        detail = " ".join(f"{k}×{v}" for k, v in sorted(got.items())) or "无发现"
        rows.append(f"{status}  {case['id']:<8} 期望 {case['expect']}  实际 {detail}")
    print("\n".join(rows))
    print("-" * 60)
    print(f"注入回归：{passed} 通过 / {failed} 失败 / 共 {passed + failed}")
    return 0 if failed == 0 else 1


def _check_expect(expect: dict, got: dict[str, int]) -> bool:
    for crit, rule in expect.items():
        if crit == "*":                          # 整体零发现（本底噪音检查 / 反例）
            if any(v > 0 for v in got.values()):
                return False
            continue
        n = got.get(crit, 0)
        if rule.startswith(">="):
            if n < int(rule[2:]):
                return False
        elif rule == "0":
            if n != 0:
                return False
        else:
            return False
    return True


if __name__ == "__main__":
    sys.exit(main())
