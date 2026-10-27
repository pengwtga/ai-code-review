"""M2 测试：webhook 验签 / 幂等 / 任务队列 / 评论锚定 / 端到端本地仿真。"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aicr.compose import compose
from aicr.engine import ReviewEngine
from aicr.webhook import (JobStore, Worker, make_handler, parse_pr_event,
                          verify_signature)


# ---------------------------------------------------------------- 单元

class TestSignature(unittest.TestCase):
    BODY = b'{"action": "opened"}'
    SECRET = "s3cret"

    def test_valid(self):
        sig = "sha256=" + hmac.new(self.SECRET.encode(), self.BODY,
                                   hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature(self.SECRET, self.BODY, sig))

    def test_invalid(self):
        self.assertFalse(verify_signature(self.SECRET, self.BODY, "sha256=deadbeef"))
        self.assertFalse(verify_signature(self.SECRET, self.BODY, None))
        self.assertFalse(verify_signature(self.SECRET, self.BODY, "sha1=abc"))

    def test_wrong_secret(self):
        sig = "sha256=" + hmac.new(b"other", self.BODY, hashlib.sha256).hexdigest()
        self.assertFalse(verify_signature(self.SECRET, self.BODY, sig))


class TestParsePR(unittest.TestCase):
    def _payload(self, action="opened"):
        return json.dumps({
            "action": action,
            "pull_request": {"number": 7, "head": {"sha": "abc123"}},
            "repository": {"name": "demo", "owner": {"login": "pengwtga"}},
        }).encode()

    def test_ok(self):
        self.assertEqual(parse_pr_event(self._payload()),
                         ("pengwtga", "demo", 7, "abc123"))

    def test_ignored_actions(self):
        for action in ("closed", "edited", "labeled"):
            self.assertIsNone(parse_pr_event(self._payload(action)))

    def test_malformed(self):
        self.assertIsNone(parse_pr_event(b"not json"))
        self.assertIsNone(parse_pr_event(b"{}"))


class TestJobStore(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.store = JobStore(tempfile.mktemp(suffix=".db"))

    def test_delivery_dedup(self):
        self.assertFalse(self.store.seen_delivery("d1"))
        self.store.record_delivery("d1", "pull_request")
        self.assertTrue(self.store.seen_delivery("d1"))

    def test_head_sha_dedup(self):
        self.assertFalse(self.store.already_reviewed("o/r", 1, "sha"))
        self.store.record_review("o/r", 1, "sha", 3)
        self.assertTrue(self.store.already_reviewed("o/r", 1, "sha"))
        self.assertFalse(self.store.already_reviewed("o/r", 1, "other"))

    def test_queue_lifecycle(self):
        self.assertIsNone(self.store.claim())
        jid = self.store.enqueue("o", "r", 1, "sha")
        job_id, job = self.store.claim()
        self.assertEqual(job, {"owner": "o", "repo": "r", "number": 1, "head_sha": "sha"})
        self.assertIsNone(self.store.claim())            # running 不可重复认领
        self.store.finish(job_id, "done", "findings=0")
        claimed = self.store.claim()                     # 其他 pending 才可认领
        self.assertIsNone(claimed)
        self.assertEqual(jid, job_id)


class TestCompose(unittest.TestCase):
    OLD = "def get_top(query):\n    items = filter_items(query)\n    return items\n"
    NEW = "def get_top(query):\n    items = filter_items(query)\n    return items[0]\n"
    DIFF = None

    @classmethod
    def setUpClass(cls):
        from aicr.diff_parser import make_unified_diff
        cls.DIFF = make_unified_diff({"svc/a.py": cls.OLD}, {"svc/a.py": cls.NEW})

    def test_anchorable_lines(self):
        from aicr.compose import anchorable_lines
        out = anchorable_lines(self.DIFF)
        self.assertIn("svc/a.py", out)
        self.assertIn(3, out["svc/a.py"])                # 变更行可锚定
        self.assertIn(1, out["svc/a.py"])                # hunk 上下文行同样可锚定
        self.assertNotIn(999, out["svc/a.py"])           # diff 之外的行不可锚定

    def test_inline_and_summary(self):
        engine = ReviewEngine()
        result = engine.review({"svc/a.py": self.OLD}, {"svc/a.py": self.NEW})
        summary, comments, skipped = compose(result, self.DIFF, "o/r#1")
        self.assertEqual(skipped, [])
        self.assertGreaterEqual(len(comments), 1)
        c = comments[0]
        self.assertEqual(c["path"], "svc/a.py")
        self.assertEqual(c["side"], "RIGHT")
        self.assertIn("aicr-bot", c["body"])
        self.assertIn("预估成本", summary)
        self.assertIn("非阻断", summary)

    def test_unanchorable_goes_to_summary(self):
        engine = ReviewEngine()
        result = engine.review({"svc/a.py": self.OLD}, {"svc/a.py": self.NEW})
        # 篡改 diff 为空 → 所有 finding 锚不上，必须降级进 summary 而不是丢掉
        summary, comments, skipped = compose(result, "", "o/r#1")
        self.assertEqual(comments, [])
        self.assertEqual(len(skipped), len(result.findings))
        self.assertIn("未锚定", summary)


# ---------------------------------------------------------------- 端到端本地仿真

class _FakeGitHub(ThreadingHTTPServer):
    """假 GitHub API：记录收到的 review 请求，返回预置 diff / 文件内容。"""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _FakeGHHandler)
        self.requests = []
        self.diff = ""
        self.file_content = ""


class _FakeGHHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        gh: _FakeGitHub = self.server
        gh.requests.append(("GET", self.path, ""))
        if "/pulls/" in self.path and self.headers.get("Accept", "").endswith(".diff"):
            body = gh.diff.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif "/contents/" in self.path:
            import base64
            content = base64.b64encode(gh.file_content.encode()).decode()
            self._json(200, {"encoding": "base64", "content": content})
        else:
            self._json(200, {"number": 1, "head": {"sha": "abc"}})

    def do_POST(self):
        gh: _FakeGitHub = self.server
        length = int(self.headers.get("Content-Length", 0))
        gh.requests.append(("POST", self.path, self.rfile.read(length).decode()))
        self._json(200, {"id": 99})


class TestEndToEnd(unittest.TestCase):
    """本地仿真：签名 webhook → 服务器 → worker → 假 GitHub API 收到行级评论。"""

    OLD = ("def get_top(query):\n"
           "    items = filter_items(query)\n"
           "    return items\n\n\n"
           "def add_tag(tag, tags=[]):\n"
           "    tags.append(tag)\n"
           "    return tags\n")
    NEW = ("def get_top(query):\n"
           "    items = filter_items(query)\n"
           "    return items[0]\n\n\n"
           "def add_tag(tag, tags=[], limit=10):\n"
           "    tags.append(tag)\n"
           "    return tags\n\n\n"
           "def ratio(success, total):\n"
           "    return success / total\n")

    def setUp(self):
        from aicr.diff_parser import make_unified_diff
        self.gh = _FakeGitHub()
        self.gh.diff = make_unified_diff({"svc/a.py": self.OLD}, {"svc/a.py": self.NEW})
        self.gh.file_content = self.NEW
        threading.Thread(target=self.gh.serve_forever, daemon=True).start()

        import tempfile
        self.store = JobStore(tempfile.mktemp(suffix=".db"))
        self.webhook_secret = "whsec"

        class _Client:      # Worker 的 client 注入：指向假 GitHub
            def __init__(self, gh):
                self.base = f"http://127.0.0.1:{gh.server_address[1]}"

        from aicr.github_api import GitHubClient
        self.client = GitHubClient(
            "fake-token",
            api_base=f"http://127.0.0.1:{self.gh.server_address[1]}")
        self.worker = Worker(self.client, self.store, poll_interval=0.05)
        self.worker.start()

        handler = make_handler(self.webhook_secret, self.store,
                               {"pengwtga/demo"})
        self.wh = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.wh.serve_forever, daemon=True).start()
        self.wh_url = f"http://127.0.0.1:{self.wh.server_address[1]}/webhook"

    def tearDown(self):
        self.worker.stop()
        self.wh.shutdown()
        self.gh.shutdown()

    def _post_webhook(self, payload: dict, sign: bool = True,
                      delivery: str = "d-1") -> tuple[int, dict]:
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json",
                   "X-GitHub-Event": "pull_request",
                   "X-GitHub-Delivery": delivery}
        if sign:
            headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(
                self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(self.wh_url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _pr_payload(self, delivery_unique=True) -> dict:
        return {"action": "opened",
                "pull_request": {"number": 7, "head": {"sha": "abc123"}},
                "repository": {"name": "demo", "owner": {"login": "pengwtga"}}}

    def _wait_done(self, timeout=15) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.store._conn() as c:
                row = c.execute("SELECT status, note FROM jobs").fetchone()
            if row and row[0] in ("done", "failed", "skipped"):
                self.assertEqual(row[0], "done", f"job 终态异常: {row}")
                return True
            time.sleep(0.1)
        return False

    def test_full_flow(self):
        code, resp = self._post_webhook(self._pr_payload())
        self.assertEqual(code, 202)
        self.assertIn("job", resp)
        self.assertTrue(self._wait_done())
        # 假 GitHub 收到：GET diff、GET contents、POST review
        posts = [r for r in self.gh.requests if r[0] == "POST"]
        self.assertTrue(posts, "应发出评论回写请求")
        review = json.loads(posts[0][2])
        self.assertEqual(review["event"], "COMMENT")     # 非阻断
        bodies = " ".join(c["body"] for c in review["comments"])
        self.assertIn("C5", bodies)                      # 可变默认参数
        self.assertIn("E1", bodies)                      # 空容器取首元素
        for c in review["comments"]:
            self.assertEqual(c["side"], "RIGHT")

    def test_duplicate_delivery(self):
        self._post_webhook(self._pr_payload())
        code, resp = self._post_webhook(self._pr_payload())   # 同 delivery 重投
        self.assertEqual(code, 200)
        self.assertEqual(resp["status"], "duplicate")

    def test_bad_signature(self):
        code, resp = self._post_webhook(self._pr_payload(), sign=False)
        self.assertEqual(code, 401)

    def test_repo_allowlist(self):
        payload = self._pr_payload()
        payload["repository"]["name"] = "other"
        code, resp = self._post_webhook(payload)
        self.assertEqual(code, 200)
        self.assertEqual(resp["status"], "ignored")

    def test_head_sha_dedup(self):
        self._post_webhook(self._pr_payload())
        self.assertTrue(self._wait_done())
        # 同 PR 同 head 再来一次（不同 delivery）
        code, resp = self._post_webhook(self._pr_payload(), delivery="d-2")
        self.assertEqual(code, 200)
        self.assertEqual(resp["status"], "already-reviewed")


if __name__ == "__main__":
    unittest.main()
