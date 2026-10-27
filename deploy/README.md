# M2 部署指南（124.222.174.133）

架构：`GitHub webhook → Caddy(:80) → aicr serve（验签/幂等/入队）→ worker → GitHub 行级评论`。
全标准库实现，`python3 -m aicr.cli serve` 即可跑，两条等价部署路径：

| 路径 | 适用 | 状态 |
|---|---|---|
| **systemd 裸跑** | 服务器已有 Python 3.12 + Caddy，零镜像拉取 | ✅ **已实机部署并验证** |
| **Docker Compose** | 换机迁移、演示、隔离边界干净 | ✅ 镜像构建 + 冒烟已验证 |

## 本次实际部署记录（systemd 路径）

| 项 | 值 |
|---|---|
| 主机 | `124.222.174.133`（Ubuntu 24.04 / Python 3.12.3） |
| 登录 | `ubuntu@124.222.174.133`（**root 已禁用**，用 `~/.ssh/id_ed25519`；`aiops_key.pem` 不对这台） |
| 代码 | `/home/ubuntu/aicr`（rsync 同步，无 `.git`） |
| 配置 | `/home/ubuntu/aicr/.env`（chmod 600），含 `WEBHOOK_SECRET` / `GITHUB_TOKEN` / `AICR_*` |
| 状态 | `/home/ubuntu/aicr/state/aicr.db`（sqlite：deliveries / jobs / reviews） |
| 日志 | `/home/ubuntu/aicr/logs/aicr.log`（systemd 追加写） |
| 监听 | `127.0.0.1:8765`（**不直接暴露公网**，只经 Caddy 反代） |
| 服务 | `aicr.service`（systemd，`Restart=always`，内存 ~13MB，硬化见下） |
| 入口 | `http://124.222.174.133/webhook`（Caddy `:80` 反代 `/webhook*` 与 `/healthz`） |

服务单元（`/etc/systemd/system/aicr.service`）的关键项：

```ini
[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/aicr
EnvironmentFile=/home/ubuntu/aicr/.env
ExecStart=/usr/bin/python3 -m aicr.cli serve --host 127.0.0.1 --port 8765 \
          --db /home/ubuntu/aicr/state/aicr.db \
          --allow pengwtga/ssrm-aiops,pengwtga/ai-code-review
Restart=always
# 硬化：审查服务只需读代码目录 + 写 sqlite
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/ubuntu/aicr/state /home/ubuntu/aicr/logs
```

Caddy（`/etc/caddy/Caddyfile`，改动前已备份 `Caddyfile.bak.<时间戳>`）：

```caddyfile
:80 {
	handle /webhook* { reverse_proxy 127.0.0.1:8765 }
	handle /healthz  { reverse_proxy 127.0.0.1:8765 }

	root * /usr/share/caddy
	file_server
}
```

### 已验证（服务器本机，经 Caddy 真实路径）

| 用例 | 结果 |
|---|---|
| `GET /healthz` | `{"ok": true}` |
| 白名单外仓库 webhook | `200 {"status":"ignored","reason":"repo not allowed"}` |
| 白名单内 + 正确签名 | `202 {"status":"queued","job":1}` |
| 同 `X-GitHub-Delivery` 重投 | `200 {"status":"duplicate"}`（不做第二次） |
| 无签名 / 错签名 | `401 {"error":"bad signature"}` |
| 同 PR 同 sha、换 delivery | 重新入队——**设计如此**：`already-reviewed` 只对「已成功评论」的 commit 生效（`reviews` 表在 job 成功后才写），失败的任务允许重试 |
| 占位 token 下的 worker | 出站 401 → job 记 `failed`，**进程不崩**，队列继续 |

### 待完成（需要你操作，两项）

1. **云安全组放行 80**（腾讯云控制台 → 安全组 → 入站规则）。
   现状：`127.0.0.1:80` 通，但公网访问返回 000，本机到 `example.com:80` 正常 →
   本地网络没问题，是安全组挡的。想直连 8765 也行，但要改 Caddy 配置并放行该端口。
2. **PAT 填进 `.env`**（当前是占位 `REPLACE_WITH_PAT`，所以 worker 出站 401）：
   GitHub → Settings → Developer settings → Personal access tokens（fine-grained）→
   只勾目标仓库的 **Contents: read** + **Pull requests: read & write**。
   填完 `sudo systemctl restart aicr`。

两项完成后，仓库 Settings → Webhooks → Add webhook：
- Payload URL：`http://124.222.174.133/webhook`
- Content type：`application/json`
- Secret：`/home/ubuntu/aicr/.env` 里的 `WEBHOOK_SECRET`
- Events：**Pull requests only**

## 路径 A：Docker Compose

```bash
cp .env.example .env        # 补齐 WEBHOOK_SECRET / GITHUB_TOKEN
docker compose -f deploy/compose.yaml up -d --build
curl -s http://127.0.0.1:8765/healthz    # {"ok": true}
```

- sqlite 状态库在 named volume `aicr-state`，升级镜像不丢幂等记录与队列
- 日志轮转 10MB×3；`docker logs -f aicr` 观察
- 已验证：镜像构建、健康检查、签名 webhook → 202 入队 → worker 出站调用
  （失败路径正确记 failed 不崩进程）

## 幂等与重试语义（面试口径）

- **delivery ID 去重**：GitHub 重投同一事件（网络抖动/5xx）不会产生第二次审查。
- **(repo, pr, head_sha) 去重**：同一 commit 只评一次；force-push 换了 sha 自动重审。
- **失败可重试**：只有成功评论的 commit 才写入 `reviews`，所以 401/超时这类失败
  不会被"去重"挡住——这是刻意的，否则一次网络抖动会让那个 commit 永久失去审查。

## 已知边界（诚实清单）

- 认证用 PAT，不是 GitHub App JWT——JWT 需 RSA 签名库，会破坏零依赖。升级 App 形态时
  只需替换 `github_api.GitHubClient` 的认证头，webhook 接收与评论回写两条路径完全复用。
- 单机单 worker + sqlite：够日均几百 PR；再大要换队列，但 M3 评测收口前不引入该复杂度。
- 无域名 → 无 HTTPS：webhook 走 `http://`，签名本身保证完整性（`X-Hub-Signature-256`），
  但传输不加密。接域名后 Caddy 自动签发证书，配置改一行。
