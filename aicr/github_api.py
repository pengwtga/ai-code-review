"""GitHub REST 客户端（M2）：拉 PR diff / 文件内容，回写行级审查评论。

- 仅用标准库 urllib，维持零依赖（私有化部署叙事的一部分）。
- 可注入 api_base：测试指向本地假服务器，生产指向 https://api.github.com。
- 认证抽象：MVP 用 PAT（Bearer）；GitHub App JWT 形态在需要组织级安装时升级，
  webhook 接收与评论回写两条路径完全复用。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request


class GitHubError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"GitHub API {status} {url}: {body[:200]}")
        self.status = status


class GitHubClient:
    def __init__(self, token: str, api_base: str = "https://api.github.com",
                 timeout: int = 30):
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    # ---- 内部 ------------------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None,
                 accept: str = "application/vnd.github+json") -> tuple[int, str]:
        url = f"{self.api_base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise GitHubError(e.code, url, e.read().decode("utf-8", errors="replace")) from None

    # ---- 读 --------------------------------------------------------------------
    def get_pr(self, owner: str, repo: str, number: int) -> dict:
        _, body = self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        return json.loads(body)

    def get_pr_diff(self, owner: str, repo: str, number: int) -> str:
        """PR 的 unified diff（GitHub 原生格式，与 engine 的 diff 解析器同构）。"""
        _, body = self._request(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}",
            accept="application/vnd.github.v3.diff")
        return body

    def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> str:
        """读取 head revision 上某文件全文（供 engine 的函数级上下文装配）。"""
        _, body = self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{path}?ref={ref}")
        data = json.loads(body)
        if data.get("encoding") != "base64":
            return ""
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    # ---- 写 --------------------------------------------------------------------
    def create_review(self, owner: str, repo: str, number: int,
                      body: str, comments: list[dict]) -> dict:
        """提交 PR review（COMMENT 事件，不 approve 不 block——M0 非阻断口径）。"""
        _, out = self._request(
            "POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            payload={"event": "COMMENT", "body": body, "comments": comments})
        return json.loads(out)

    def create_issue_comment(self, owner: str, repo: str, number: int, body: str) -> dict:
        _, out = self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/comments",
            payload={"body": body})
        return json.loads(out)
