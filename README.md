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

## AgentScope Studio（可视化追踪面板）

```bash
npm install -g @agentscope/studio   # 一次性安装（需 Node >= 20）
as_studio                           # 启动，默认 http://localhost:3000
```

`.env` 设置 `AGENTFORGE_STUDIO_URL=http://localhost:3000` 后，执行会自动上报 trace。
留空该变量则完全零开销。

> 沙箱/受限环境启动方式：`HOME="$PWD/.studio-data" as_studio`（把 SQLite 数据目录重定向到项目内）。
