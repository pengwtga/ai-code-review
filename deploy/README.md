# M2 部署指南（目标：124.222.174.133）

架构：`GitHub webhook → aicr serve（验签/幂等/入队）→ worker → GitHub 行级评论`。
全标准库实现，提供两条等价部署路径：

| 路径 | 适用 | 命令量 |
|---|---|---|
| **Docker（推荐）** | 换机迁移、演示、隔离边界干净 | 2 条 |
| **systemd 裸跑** | 不想装 Docker / 极简环境 | ~10 条 |

## 路径 A：Docker Compose（已构建验证）

```bash
# 服务器（或本地）上
git clone <repo> && cd ai-code-review
cp .env.example .env        # 补齐 WEBHOOK_SECRET / GITHUB_TOKEN
docker compose -f deploy/compose.yaml up -d --build
curl -s http://127.0.0.1:8765/healthz    # {"ok": true}
```

- sqlite 状态库在 named volume `aicr-state` 里，升级镜像不丢幂等记录与任务队列
- 日志轮转 10MB×3；`docker logs -f aicr` 观察
- 已验证：镜像构建、健康检查、签名 webhook → 202 入队 → worker 出站调用
  （失败路径正确记 failed 不崩进程）

## 路径 B：systemd 裸跑

零依赖的代价是环境自管——这条路要求服务器 Python ≥ 3.9，适合不想引入
Docker daemon 的极简环境。

## 1. 准备 GitHub 侧（一次性）

1. **PAT**：GitHub → Settings → Developer settings → Personal access tokens
   （fine-grained 更好），范围只要目标仓库的 **Contents: read + Pull requests: read&write**。
2. **Webhook secret**：随便生成一串（`openssl rand -hex 24`），GitHub 和服务器各存一份。
3. **Webhook**：仓库 → Settings → Webhooks → Add：
   - Payload URL：`http://124.222.174.133:8765/webhook`（有域名后换 https）
   - Content type：`application/json`
   - Secret：上面那串
   - Events：**Pull requests only**
   - Active ✅

## 2. 服务器侧（systemd 路径）

```bash
# 创建运行用户与目录
useradd -r -s /usr/sbin/nologin aicr
mkdir -p /opt/aicr && chown aicr:aicr /opt/aicr

# 上传代码（本地执行）
rsync -av --exclude '.git' --exclude '.env' --exclude '*_state.db' \
    ~/Desktop/面试/AI研发/ai-code-review/ root@124.222.174.133:/opt/aicr/

# 服务器上配置环境
cat > /opt/aicr/.env <<'EOF'
WEBHOOK_SECRET=<第1步生成的secret>
GITHUB_TOKEN=<PAT>
GITHUB_API_BASE=https://api.github.com
AICR_LLM=            # 留空=仅确定性层；=1 启用 L2 agent（另需 AICR_API_*）
EOF
chmod 600 /opt/aicr/.env && chown aicr:aicr /opt/aicr/.env

# systemd 安装
cp /opt/aicr/deploy/aicr.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now aicr
curl -s http://127.0.0.1:8765/healthz     # {"ok": true} 即存活

# 防火墙/安全组放行 8765（仅 GitHub webhook 源更佳，可事后从日志取 IP 段）
```

## 3. 验证

```bash
# 服务器上观察
journalctl -u aicr -f
# 开一个测试 PR → 应看到「入队 job=…」→「完成 …#N 发现 M」→ PR 出现行级评论
```

幂等自检：同一 commit 重开 webhook 重投（GitHub 会重试），日志只应出现一次
「完成」；force-push 后 head_sha 变化，自动重审。

## 4. 已知边界（诚实清单）

- 认证用 PAT，不是 GitHub App JWT——组织级安装/权限收窄需要 App 形态时升级，
  webhook 接收与评论回写两条路径完全复用（`github_api.GitHubClient` 是唯一差异点）。
- 单机单 worker：任务表是 sqlite，够日均几百 PR；再大需要换队列，但验收收口
  前（M3 评测）不引入这个复杂度。
- 服务器在中国大陆：`api.github.com` 直连偶发超时，失败任务在 jobs 表标
  `failed`，重投 delivery 或 force-push 即重试。
