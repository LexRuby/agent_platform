# AgentForge

基于 AgentScope 2.0 的智能体服务平台（agent-service 形态，多租户，Web UI :30000）。

## 目录

```
agent_service_app.py    生产部署入口（AgentScope 官方 agent-service，多租户，需 Redis）
app/                    核心模块
  agent_factory.py      构建 Agent（Ark/DashScope + Toolkit + MCP）
  agent_runner.py       执行器（上下文注入 + fake-llm 演示开关）
  agent_version.py      版本封板/发版/恢复（冻结后 PATCH 返回 403）
  leader_team.py        主理人（大A）与成员（小A）团队编排
  team_archive.py       任务完成后工作流归档复用
  auth.py               登录与会话
  registry.py           MCP/Agent 注册表
  settings.py           配置加载
  smart_writing.py      智能写作 MCP 服务（:30110，systemd 常驻）
  tools/                内网中台工具薄封装，含重试/超时/service token
mcp_registry/           MCP 注册文件（*.json，is_stateful: true）
webui-src/              前端源码（React + TypeScript，构建产物入 webui/）
scripts/                build_webui（一键重建前端）/ smoke 脚本
tests/                  全量回归测试（不依赖真实外部服务）
```

## 快速开始

```bash
cd agentforge
pip install -r requirements.txt
cp .env.example .env   # 填 ARK_API_KEY / DASHSCOPE_API_KEY、Redis 等

# 跑测试（不需要真实 LLM / 内网）
python -m pytest tests/ -q

# 起服务
python agent_service_app.py   # 默认 :30000
```

论文写作小 A 会自动获得有界的期刊检索和 `anysearch_search` 联网搜索。
AnySearch 当前可免 Key 调用；如服务端要求鉴权，在 `.env` 配置可选的
`ANYSEARCH_API_KEY`。每种检索在单个会话内最多请求两次，首次失败后熔断。

## 前端

```bash
bash scripts/build_webui.sh   # 源码在 webui-src/，产物输出到 webui/
```

> 沙箱/受限环境下若系统 node 过旧，可用 Docker 构建：
> `docker run --rm -v "$PWD/webui-src:/app" -w /app node:20-alpine npm run build`
> 然后把 `webui-src/dist/*` 拷入 `webui/`。

## 部署（systemd）

- `agentforge-svc.service`：主服务（agent_service_app.py，:30000）
- `agentforge-smart-writing.service`：智能写作 MCP（:30110）

重启主服务：`sudo systemctl restart agentforge-svc`

## 可观测性（Jaeger + Prometheus）

一键部署链路追踪与指标后端（Docker Compose，数据落盘，重启不丢）：

```bash
bash deploy/observability/start.sh     # 启动（自动拉起容器并等就绪）
bash deploy/observability/restart.sh   # 重启（trace/指标不丢）
bash deploy/observability/stop.sh      # 停止（保留数据）
bash deploy/observability/stop.sh --purge  # 彻底删除（含全部历史数据，慎用）
```

启动后打开：

- Jaeger UI（调用链）：http://116.204.102.229:16686 ，Service 选 `agentforge`
- Prometheus（指标）：http://116.204.102.229:9090 ，常用查询
  `agentforge_http_requests_total`、`agentforge_llm_tokens_total`、`agentforge_llm_latency_seconds_bucket`

说明：

- 平台通过 `.env` 上报：`AGENTFORGE_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces`、
  `AGENTFORGE_OTLP_METRICS_ENDPOINT=http://127.0.0.1:9090/api/v1/otlp/v1/metrics`；
  `deploy/observability/start.sh` 会检查并提示缺失的接入配置，留空则零开销
  （改后需 `sudo systemctl restart agentforge-svc` 生效）
- Jaeger 用 badger 磁盘存储（命名卷），Prometheus 历史保留 7 天；服务器重启后
  容器自动拉起（`restart: unless-stopped`），数据完整保留
- 前置条件：Docker + docker compose v2；无需外网（已用国内镜像源）

## AgentScope Studio（可视化追踪面板）

```bash
npm install -g @agentscope/studio   # 一次性安装（需 Node >= 20）
as_studio                           # 启动，默认 http://localhost:3000
```

`.env` 设置 `AGENTFORGE_STUDIO_URL=http://localhost:3000` 后，执行会自动上报 trace。
留空该变量则完全零开销。

> 沙箱/受限环境启动方式：`HOME="$PWD/.studio-data" as_studio`（把 SQLite 数据目录重定向到项目内）。
