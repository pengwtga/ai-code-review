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


if __name__ == "__main__":
    unittest.main()
