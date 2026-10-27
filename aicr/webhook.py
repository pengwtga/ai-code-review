"""Webhook 服务器 + 持久化任务队列 + worker（M2 核心）。

架构与约束（对应竞品分析里的三条卖点）：
- 验签：X-Hub-Signature-256 HMAC-SHA256，常量时间比较——伪造请求进不来。
- 异步：webhook 有 10 秒超时窗口，四路审查需要更久——响应 202 立即入队，
  worker 后台处理。
- 幂等：delivery ID 去重（重投无害）+ (repo, pr, head_sha) 去重（同一 commit
  只评一次，force-push 换 sha 自然重审）。
- 持久化：sqlite 任务表，worker 轮询认领——进程重启不丢任务，天然单机排队。
- 作用域：repo 白名单；bot 自己触发的评论事件忽略（防自激循环）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .engine import ReviewEngine
from .github_api import GitHubClient, GitHubError
from .compose import compose


# ---------------------------------------------------------------- 持久化存储

class JobStore:
    """sqlite：deliveries（幂等）+ reviews（head 去重）+ jobs（任务队列）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS deliveries(
                    id TEXT PRIMARY KEY, received_at TEXT, event TEXT);
                CREATE TABLE IF NOT EXISTS reviews(
                    repo TEXT, pr INTEGER, head_sha TEXT, reviewed_at TEXT,
                    findings INTEGER, PRIMARY KEY(repo, pr, head_sha));
                CREATE TABLE IF NOT EXISTS jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT, repo TEXT, number INTEGER, head_sha TEXT,
                    status TEXT DEFAULT 'pending',   -- pending|running|done|skipped|failed
                    note TEXT, created_at REAL, updated_at REAL);
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            """)

    # ---- 幂等 ----
    def seen_delivery(self, delivery_id: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            return row is not None

    def record_delivery(self, delivery_id: str, event: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO deliveries(id, received_at, event) VALUES(?,?,?)",
                      (delivery_id, time.strftime("%Y-%m-%dT%H:%M:%S"), event))

    def already_reviewed(self, repo: str, pr: int, head_sha: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM reviews WHERE repo=? AND pr=? AND head_sha=?",
                             (repo, pr, head_sha)).fetchone() is not None

    def record_review(self, repo: str, pr: int, head_sha: str, findings: int) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)",
                      (repo, pr, head_sha, time.strftime("%Y-%m-%dT%H:%M:%S"), findings))

    # ---- 任务队列 ----
    def enqueue(self, owner: str, repo: str, number: int, head_sha: str) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO jobs(owner, repo, number, head_sha, created_at, updated_at)"
                            " VALUES(?,?,?,?,?,?)",
                            (owner, repo, number, head_sha, time.time(), time.time()))
            return cur.lastrowid

    def claim(self) -> tuple[int, dict] | None:
        """认领一条 pending 任务（原子：UPDATE ... WHERE status='pending' 竞争者输）。"""
        with self._conn() as c:
            row = c.execute("SELECT id, owner, repo, number, head_sha FROM jobs"
                            " WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            job_id = row[0]
            cur = c.execute(
                "UPDATE jobs SET status='running', updated_at=?"
                " WHERE id=? AND status='pending'", (time.time(), job_id))
            if cur.rowcount == 0:
                return None
        return job_id, {"owner": row[1], "repo": row[2],
                        "number": row[3], "head_sha": row[4]}

    def finish(self, job_id: int, status: str, note: str = "") -> None:
        with self._conn() as c:
            c.execute("UPDATE jobs SET status=?, note=?, updated_at=? WHERE id=?",
                      (status, note, time.time(), job_id))


# ---------------------------------------------------------------- webhook 处理

def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """GitHub webhook 签名校验（sha256=...，常量时间比较）。"""
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[7:], digest)


PR_ACTIONS = {"opened", "synchronize"}


def parse_pr_event(body: bytes) -> tuple[str, str, int, str] | None:
    """提取 (owner, repo, pr_number, head_sha)；非 PR opened/synchronize 返回 None。"""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    action = payload.get("action")
    if action not in PR_ACTIONS:
        return None
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    owner = (repo.get("owner") or {}).get("login")
    sha = (pr.get("head") or {}).get("sha")
    if not (owner and repo.get("name") and pr.get("number") and sha):
        return None
    return owner, repo["name"], int(pr["number"]), sha


def make_handler(secret: str, store: JobStore, allow_repos: set[str],
                 log=print):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):          # 静默默认访问日志
            pass

        def do_POST(self):
            if self.path.rstrip("/") != "/webhook":
                self._reply(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            delivery = self.headers.get("X-GitHub-Delivery", "")
            event = self.headers.get("X-GitHub-Event", "")

            if not verify_signature(secret, body, self.headers.get("X-Hub-Signature-256")):
                log(f"[aicr] 验签失败 delivery={delivery[:12]}")
                self._reply(401, {"error": "bad signature"})
                return
            if store.seen_delivery(delivery):        # 重投幂等
                self._reply(200, {"status": "duplicate", "delivery": delivery})
                return
            parsed = parse_pr_event(body)
            if parsed is None or event != "pull_request":
                self._reply(200, {"status": "ignored", "event": event})
                return
            owner, repo, number, sha = parsed
            full = f"{owner}/{repo}"
            if allow_repos and full not in allow_repos:
                log(f"[aicr] 仓库不在白名单：{full}")
                self._reply(200, {"status": "ignored", "reason": "repo not allowed"})
                return
            if store.already_reviewed(full, number, sha):
                self._reply(200, {"status": "already-reviewed", "head": sha[:8]})
                return

            store.record_delivery(delivery, event)
            job_id = store.enqueue(owner, repo, number, sha)
            log(f"[aicr] 入队 job={job_id} {full}#{number} @{sha[:8]}")
            self._reply(202, {"status": "queued", "job": job_id})

        def do_GET(self):
            if self.path.rstrip("/") == "/healthz":
                self._reply(200, {"ok": True})
                return
            self._reply(404, {"error": "not found"})

        def _reply(self, code: int, payload: dict):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


# ---------------------------------------------------------------- worker

class Worker(threading.Thread):
    """后台审查线程：认领 job → 拉 diff/文件 → 引擎审查 → 行级评论回写。"""

    def __init__(self, client: GitHubClient, store: JobStore,
                 confidence_threshold: float = 0.55, poll_interval: float = 1.0,
                 log=print):
        super().__init__(daemon=True, name="aicr-worker")
        self.client = client
        self.store = store
        self.poll_interval = poll_interval
        self.log = log
        self.threshold = confidence_threshold
        self.enable_llm = os.environ.get("AICR_LLM", "") in ("1", "true", "yes")
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self.log("[aicr] worker 启动")
        while not self._stop.is_set():
            claimed = self.store.claim()
            if claimed is None:
                self._stop.wait(self.poll_interval)
                continue
            job_id, job = claimed
            try:
                n = self._process(job)
                self.store.finish(job_id, "done", f"findings={n}")
                self.store.record_review(f"{job['owner']}/{job['repo']}",
                                         job["number"], job["head_sha"], n)
                self.log(f"[aicr] 完成 {job['owner']}/{job['repo']}#{job['number']} 发现 {n}")
            except GitHubError as e:
                self.store.finish(job_id, "failed", str(e))
                self.log(f"[aicr] GitHub API 失败 job={job_id}: {e}")
            except Exception as e:                       # 引擎崩了不能丢任务记录
                self.store.finish(job_id, "failed",
                                  f"{type(e).__name__}: {e}")
                self.log(f"[aicr] job={job_id} 异常：{traceback.format_exc(limit=3)}")

    def _process(self, job: dict) -> int:
        owner, repo, number = job["owner"], job["repo"], job["number"]
        diff_text = self.client.get_pr_diff(owner, repo, number)
        if not diff_text.strip():
            return 0

        # head revision 的 .py 全文（函数级上下文装配；拉不到的文件由引擎降级）
        sources: dict[str, str] = {}
        from .diff_parser import parse_unified_diff
        for fd in parse_unified_diff(diff_text):
            path = fd.new_path or fd.old_path
            if not path.endswith(".py"):
                continue
            try:
                sources[path] = self.client.get_file_content(
                    owner, repo, path, job["head_sha"])
            except GitHubError:
                sources[path] = ""                       # 删除的文件等场景

        engine = ReviewEngine(confidence_threshold=self.threshold,
                              enable_llm=self.enable_llm)
        result = engine.review_diff(diff_text, sources=sources)
        summary, comments, _skipped = compose(result, diff_text,
                                              repo_label=f"{owner}/{repo}#{number}")
        if comments:
            self.client.create_review(owner, repo, number, summary, comments)
        else:
            self.client.create_issue_comment(owner, repo, number, summary)
        return result.stats.findings


# ---------------------------------------------------------------- serve 入口

def serve(host: str, port: int, secret: str, token: str,
          db_path: str, allow_repos: set[str],
          api_base: str = "https://api.github.com") -> ThreadingHTTPServer:
    store = JobStore(db_path)
    worker = Worker(GitHubClient(token, api_base), store,
                    log=lambda m: print(m, flush=True))
    worker.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(secret, store, allow_repos,
                                                           log=lambda m: print(m, flush=True)))
    return httpd
