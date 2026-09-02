# AgentForge 工单服务（W1 骨架）

智汇平台第一周交付：基于 AgentScope 2.0 的工单式智能体执行服务。
实现"自动环节跑智能体、人工环节挂起等人、按钮式人工干预"的最小闭环。

## 架构（三层）

```
templates/*.yaml        流程模板（静态默认流程，YAML 声明式）
        │ instantiate
        ▼
TaskInstance（工单）     history（不可变事实）｜current（可取消重做）｜planned（可增删换序）
        │ engine 逐环节驱动
        ▼
AgentScope Agent        自动环节：ReAct + 内网中台工具；人工环节：waiting_human 挂起
```

- 自动环节：每步一个 Agent，环节指令即任务，上游产出注入上下文（`app/agent_runner.py`）
- 人工环节：任务停在 `waiting_human`，前端待办收件箱轮询 `GET /tasks?status=waiting_human&assignee=xxx`
- 干预（按钮式）：submit（通过/提交）、skip（跳过）、redo（重做：失败当前环节直接重跑；已完成环节插入 `xxx__redoN` 副本）、reassign（改派）。全部记入 `interventions` 审计日志

## 目录

```
agent_service_app.py    生产部署形态（AgentScope 官方 agent-service，多租户，需 Redis）
app/main.py             工单 FastAPI 服务（W1 演示形态）
app/agent_factory.py    构建 Agent（DashScope + Toolkit）
app/agent_runner.py     自动环节执行器（上下文注入 + fake-llm 演示开关）
app/tools/              内网中台工具薄封装（路径 A），含重试/超时/service token
app/workflow/           models（工单模型）/ loader（YAML）/ store（JSON 落盘）/ engine（引擎）
scripts/                mock_midplatform（假中台）/ smoke_tools（工具层冒烟）
templates/              流程模板（高考志愿）
tests/                  工作流引擎测试（stub 智能体，不需要 LLM）
```

## 快速开始

```bash
cd agentforge
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填 DASHSCOPE_API_KEY、内网中台地址

# 跑测试（不需要 LLM / 内网）
.venv/bin/python -m pytest tests/ -q

# 起服务
uvicorn app.main:app --port 8100
```

## 本地 E2E（无需 API Key）

```bash
# 终端 1：假中台（模拟内网四大能力服务，真实端点对齐后删除）
.venv/bin/python -m scripts.mock_midplatform

# 终端 2：工单服务（fake-llm 模式，跳过大模型调用）
AGENTFORGE_FAKE_LLM=1 .venv/bin/python -m uvicorn app.main:app --port 8100

# 终端 3：驱动完整生命周期
curl -X POST localhost:8100/tasks -H 'Content-Type: application/json' -d '{
  "template": "gaokao_volunteer",
  "variables": {"province": "河南省", "subject_type": "物理类", "score": 621,
                "preference": "计算机相关，倾向北京上海"}}'
curl 'localhost:8100/tasks?status=waiting_human&assignee=counselor'   # 待办收件箱
curl -X POST localhost:8100/tasks/{task_id}/submit -H 'Content-Type: application/json' -d '{
  "step_id": "confirm_profile", "output": "画像确认无误，补充：不接受偏远地区",
  "actor": "counselor"}'

# 工具层直连验证（httpx / 认证头 / 重试链路，走假中台）
MIDPLATFORM_BASE_URL=http://127.0.0.1:9000 .venv/bin/python -m scripts.smoke_tools
```

fake-llm 模式下自动环节输出带 `[demo]` 前缀，代表该环节本应调用大模型；
去掉 `AGENTFORGE_FAKE_LLM=1` 并在 `.env` 填 `ARK_API_KEY`（豆包）或 `DASHSCOPE_API_KEY`（百炼）即为真实模式（见下节）。

> 真实模式提示：submit 人工环节后引擎会同步跑完后续自动环节再返回，长流程下 HTTP 请求可能超时——
> 服务端协程不会中断，任务照常推进，轮询 `GET /tasks/{id}` 看最终状态即可；彻底解决是 W3 的异步化。

## E2E 演示（高考志愿）

```bash
# 1. 创建任务：自动跑完检索 → 停在人工确认
curl -X POST localhost:8100/tasks -H 'Content-Type: application/json' -d '{
  "template": "gaokao_volunteer",
  "variables": {"province": "河南省", "subject_type": "物理类", "score": 621,
                "preference": "计算机相关，倾向北京上海"}
}'

# 2. 待办收件箱
curl 'localhost:8100/tasks?status=waiting_human&assignee=counselor'

# 3. 提交人工环节 → 自动继续生成报告直至完成
curl -X POST localhost:8100/tasks/{task_id}/submit -H 'Content-Type: application/json' -d '{
  "step_id": "confirm_profile", "output": "画像确认无误，补充：不接受偏远地区",
  "actor": "counselor"
}'

# 4. 干预：跳过 / 重做 / 改派
curl -X POST localhost:8100/tasks/{task_id}/skip   -d '{"step_id": "...", "actor": "boss"}'
curl -X POST localhost:8100/tasks/{task_id}/redo   -d '{"step_id": "...", "actor": "boss"}'
curl -X POST localhost:8100/tasks/{task_id}/reassign -d '{"step_id": "...", "assignee": "bob", "actor": "boss"}'
```

## 对接内网中台（改这两个地方）

1. `app/tools/*.py` 每个工具类的 `endpoint`（对齐中台真实路由）
2. 工具 `description` 已按"何时该用/何时不该用"打磨过，暴露新端点时保持这个写法——
   它直接决定 Agent 调用准确率

MIDPLATFORM_TOKEN 以 Bearer 头注入；同内网低延迟，缓存暂不需要（W3 再加）。

## AgentScope Studio（可视化追踪面板）

```bash
npm install -g @agentscope/studio   # 一次性安装（需 Node >= 20）
as_studio                           # 启动，默认 http://localhost:3000
```

`.env` 设置 `AGENTFORGE_STUDIO_URL=http://localhost:3000` 后，每个自动环节的执行会自动上报 trace：
打开 Studio → 左侧 Traces → 点开任意一条，可见完整调用链（Agent → LLM 调用 → 工具调用 → LLM 调用）、
每次调用的输入输出全文、token 用量与耗时。排障"模型为什么调这个工具/返回了什么"主要靠它。
留空该变量则完全零开销。

> 沙箱/受限环境启动方式：`HOME="$PWD/.studio-data" as_studio`（把 SQLite 数据目录重定向到项目内）。

## 路线对齐（见 trae/2026-09-02_AgentScope选型与提速分析.md）

- W1（本骨架）：工单引擎 + 检索/写作工具 + 高考志愿 E2E + 按钮式干预 ✅
- W2：可视化/标注工具接入、长期记忆（ReMe/Mem0）、表单式干预（编辑 planned）
- W3：异步长任务、Redis 精确缓存（观测已提前完成：Studio tracing 见上节）
- 之后：控制台 UI、拖拽小图编辑器（2-4 周）、RBAC、agent-service 生产化

## 已知边界（W1 有意的简化）

- 自动环节同步执行（uvicorn worker 内），失败任务停住等 redo；异步化在 W3
- 每环节独立 Agent，跨环节靠注入上下文；会话级连续记忆在 W2 接入
- 存储为单文件 JSON，多进程部署前换 SQLite/PostgreSQL
- `agent_service_app.py` 是官方多租户部署入口，与本工单服务互补，切换成本一天内
