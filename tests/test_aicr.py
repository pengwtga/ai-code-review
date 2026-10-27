"""单元测试：python -m unittest discover -s tests -v"""

import unittest

from aicr.diff_parser import parse_unified_diff, make_unified_diff, changed_line_numbers
from aicr.engine import ReviewEngine
from aicr.context import FileContext
from aicr.detectors import detect_c5, detect_e4
from aicr.criteria_schema import Finding


SAMPLE_DIFF = """diff --git a/services/a.py b/services/a.py
--- a/services/a.py
+++ b/services/a.py
@@ -1,4 +1,6 @@
 def top(q):
-    return q
+    items = filter_items(q)
+    return items[0]
"""
OLD_A = "def top(q):\n    return q\n"
NEW_A = "def top(q):\n    items = filter_items(q)\n    return items[0]\n"


class TestDiffParser(unittest.TestCase):
    def test_parse_and_anchor(self):
        fds = parse_unified_diff(SAMPLE_DIFF)
        self.assertEqual(len(fds), 1)
        fd = fds[0]
        self.assertEqual(fd.new_path, "services/a.py")
        self.assertEqual(changed_line_numbers(fd), {2, 3})

    def test_roundtrip(self):
        diff = make_unified_diff({"services/a.py": OLD_A}, {"services/a.py": NEW_A})
        fds = parse_unified_diff(diff)
        self.assertEqual(changed_line_numbers(fds[0]), {2, 3})


class TestDetectors(unittest.TestCase):
    def test_c5_flags_mutable_default(self):
        src = "def add(tag, tags=[]):\n    tags.append(tag)\n"
        fc = FileContext("a.py", src, {1})
        out = detect_c5(fc)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].criterion_id, "C5")

    def test_c5_skip_none_default(self):
        src = "def add(tag, tags=None):\n    tags = tags if tags is not None else []\n"
        fc = FileContext("a.py", src, {1, 2})
        self.assertEqual(detect_c5(fc), [])

    def test_e4_flags_silent_except(self):
        src = "try:\n    x()\nexcept Exception:\n    pass\n"
        fc = FileContext("a.py", src, {2, 3, 4})
        out = detect_e4(fc)
        self.assertEqual(len(out), 1)

    def test_e4_skip_narrow_logged(self):
        src = "try:\n    x()\nexcept FileNotFoundError as e:\n    log.warning('%s', e)\n"
        fc = FileContext("a.py", src, {2, 3, 4})
        self.assertEqual(detect_e4(fc), [])


class TestEngine(unittest.TestCase):
    def test_review_flags_and_evidence(self):
        engine = ReviewEngine()
        result = engine.review({"a.py": OLD_A}, {"a.py": NEW_A})
        self.assertTrue(any(f.criterion_id == "E1" for f in result.findings))
        for f in result.findings:
            self.assertTrue(f.evidence)          # 无引用不渲染

    def test_clean_diff_no_findings(self):
        engine = ReviewEngine()
        old = "def f(x):\n    return x\n"
        new = "def f(x):\n    if not x:\n        return None\n    return x\n"
        result = engine.review({"m.py": old}, {"m.py": new})
        self.assertEqual(result.findings, [])

    def test_non_python_ignored(self):
        engine = ReviewEngine()
        result = engine.review({"r.md": "# a\n"}, {"r.md": "# b\n"})
        self.assertEqual(result.findings, [])


class TestFindingContract(unittest.TestCase):
    def test_unknown_criterion_rejected(self):
        with self.assertRaises(ValueError):
            Finding("X9", "a.py", 1, "major", 0.9, "m", ["a.py:1"])

    def test_empty_evidence_rejected(self):
        with self.assertRaises(ValueError):
            Finding("C1", "a.py", 1, "major", 0.9, "m", [])


# ---------------------------------------------------------------- L2 agent（M1.5）

import json

from aicr.tools import ReviewToolbox
from aicr import llm as llm_mod

SOURCES = {
    "svc/a.py": "def helper(x):\n    return x * 2\n\n\ndef handler(q):\n    v = helper(q)\n    return v[0]\n",
    "svc/b.py": "from svc.a import helper\n\ndef other():\n    return helper(3)\n",
}


class TestToolbox(unittest.TestCase):
    def test_find_symbol(self):
        tb = ReviewToolbox(SOURCES)
        out = json.loads(tb.call("find_symbol", {"name": "helper"}))
        self.assertEqual(len(out), 1)            # 仅 a.py 的定义；b.py 是 import 不算
        self.assertEqual(out[0]["file"], "svc/a.py")

    def test_get_function_body_scoped(self):
        tb = ReviewToolbox(SOURCES)
        out = json.loads(tb.call("get_function_body", {"file": "svc/a.py", "line": 5}))
        self.assertIn("def handler", out["source"])
        out2 = json.loads(tb.call("get_function_body", {"file": "etc/passwd", "line": 1}))
        self.assertIn("error", out2)             # 作用域受限：变更集合外拒绝

    def test_list_callers_and_search(self):
        tb = ReviewToolbox(SOURCES)
        callers = json.loads(tb.call("list_callers", {"symbol": "helper"}))
        self.assertEqual(len(callers), 2)
        hits = json.loads(tb.call("search_repo", {"pattern": "helper"}))
        self.assertTrue(all("helper" in h["text"].lower() for h in hits))

    def test_budget_enforced(self):
        tb = ReviewToolbox(SOURCES, max_calls=2)
        tb.call("search_repo", {"pattern": "a"})
        tb.call("search_repo", {"pattern": "b"})
        out = json.loads(tb.call("search_repo", {"pattern": "c"}))
        self.assertIn("预算已用尽", out["error"])
        self.assertEqual(tb.calls_made, 2)

    def test_unknown_tool_rejected(self):
        tb = ReviewToolbox(SOURCES)
        out = json.loads(tb.call("write_file", {"path": "x", "content": "y"}))
        self.assertIn("未注册工具", out["error"])


class _FakeTransport:
    """模拟模型：第一轮请求工具，之后输出结论。可注入固定行为。"""

    def __init__(self, script):
        self.script = list(script)               # 每轮返回的 message dict
        self.calls = 0

    def __call__(self, messages, tools):
        self.calls += 1
        return {"choices": [{"message": self.script.pop(0)}]}


def _tool_call(name, args, tid="t1"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": tid, "function": {"name": name,
                                                    "arguments": json.dumps(args)}}]}


def _final(content):
    return {"role": "assistant", "content": content}


class TestAgentLoop(unittest.TestCase):
    def _unit(self, tb):
        return llm_mod.agent_review_unit("svc/a.py", SOURCES["svc/a.py"], 5,
                                         [6], tb, transport=self.transport)

    def test_tool_then_answer(self):
        self.transport = _FakeTransport([
            _tool_call("list_callers", {"symbol": "helper"}),
            _final(json.dumps([{"criterion_id": "C2", "line": 6, "severity": "major",
                                "confidence": 0.7, "message": "复制模板变量错位",
                                "evidence_refs": ["svc/a.py:6", "tool:list_callers:2 个调用点"]}],
                               ensure_ascii=False)),
        ])
        tb = ReviewToolbox(SOURCES)
        findings, trace = self._unit(tb)
        self.assertEqual(trace["tool_calls"], 1)
        self.assertEqual(trace["tools_used"], {"list_callers": 1})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].criterion_id, "C2")
        self.assertEqual(len(tb.trace), 1)       # 全程可回放

    def test_whitelist_filtered(self):
        self.transport = _FakeTransport([
            _final(json.dumps([
                {"criterion_id": "C2", "line": 1, "severity": "major", "confidence": 0.8,
                 "message": "x", "evidence_refs": ["a:1"]},
                {"criterion_id": "N1", "line": 1, "severity": "major", "confidence": 0.8,
                 "message": "L2 无权报 N 路", "evidence_refs": ["a:1"]},
                {"criterion_id": "C2", "line": 2, "severity": "major", "confidence": 0.8,
                 "message": "无证据", "evidence_refs": []},
            ])),
        ])
        findings, _ = self._unit(ReviewToolbox(SOURCES))
        self.assertEqual([f.criterion_id for f in findings], ["C2"])   # N1 越权丢弃 + 无证据丢弃

    def test_budget_forces_final_answer(self):
        # 模型死循环要工具：4 轮迭代上限后强制收敛
        self.transport = _FakeTransport([_tool_call("search_repo", {"pattern": "x"})] * 10)
        findings, trace = self._unit(ReviewToolbox(SOURCES))
        self.assertEqual(trace["iterations"], llm_mod._MAX_ITERATIONS)
        self.assertEqual(trace["tool_calls"], llm_mod._MAX_ITERATIONS)
        self.assertEqual(findings, [])           # 无结论输出 → 空，但循环正常终止

    def test_tool_error_does_not_crash_loop(self):
        self.transport = _FakeTransport([
            _tool_call("get_function_body", {"file": "svc/a.py"}),   # 缺 line 参数
            _final("[]"),
        ])
        findings, trace = self._unit(ReviewToolbox(SOURCES))
        self.assertEqual(trace["tool_calls"], 1)  # 工具报错被捕获为 error JSON
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
