# AgentForge 交接与部署文档

> 生成时间：2026-09-02。本文档自包含全部背景、现状、部署步骤与已知坑，供在服务器侧独立执行，无需额外上下文。

***

## 一、项目背景（是什么、为什么）

**项目**：智汇平台——基于开源框架 AgentScope 2.0 的智能体执行平台。
**核心形态**：工单式（BPM 审批流模型）任务处理——任务由多个环节组成，自动环节由大模型智能体执行（可调用内网四大能力服务），人工环节挂起等待提交，支持运行时人工干预。

**关键决策记录**（详见本地 trae/2026-09-02\_AgentScope选型与提速分析.md）：

| 决策点    | 结论                                                |
| ------ | ------------------------------------------------- |
| 框架     | AgentScope 2.0（Python），不用自研运行时                    |
| 四大能力服务 | 用户已有：内网 FastAPI 中台（检索/标注/可视化/写作），本服务仅做工具薄封装对接     |
| 流程定义   | YAML 声明式模板（拖拽画布后置，非 MVP 范围）                       |
| 人工干预   | 按钮式（submit/skip/redo/reassign）已实现；表单式（W2）、拖拽式（后置） |
| 大模型    | 火山方舟豆包（OpenAI 兼容协议），远端 API，**无本地推理，不需要 GPU**      |

**三层架构**：

```
templates/*.yaml     流程模板（静态默认流程）
       │ instantiate
       ▼
TaskInstance 工单    history(不可变) | current(可重做) | planned(可调整) + interventions 审计
       │ engine 逐环节驱动
       ▼
AgentScope Agent     自动环节：ReAct + 内网中台工具；人工环节：waiting_human 挂起
```

**当前状态（W1 已完成并验证）**：

- 工单引擎（创建/提交/跳过/重做/改派 + 持久化 + 审计）✅ 单测 10/10

- 内网中台工具薄封装：检索×2 + 写作×2（httpx + Bearer + 重试）✅

- 高考志愿场景模板（检索→人工确认→报告）✅

- 真实 LLM E2E 全流程通过（豆包 turbo）：模型自主调工具、吸收人工约束生成报告 ✅

- AgentScope Studio tracing 接入验证 ✅（可选组件）
- W2：控制台（MCP 注册/Agent 编排/工作空间/发布，形态A 自带单页 :8200）✅
- W2：**官方 Web UI 形态 B 上线**（agent-service :8300，MCP/Skill Hub、Agent Team、多会话、人工干预、知识库、定时任务）✅ E2E PASS

***

## 二、代码结构

```
agentforge/
├── agent_service_app.py   部署形态B：AgentScope 官方 agent-service + Web UI（:8300，需 Redis）★ 推荐
├── app/main.py            部署形态A：工单 FastAPI 服务（:8200）
├── app/agent_factory.py   构建 Agent（provider 分支 doubao|dashscope + TracingMiddleware）
├── app/agent_runner.py    自动环节执行器（上下文注入 + fake-llm 开关）
├── app/settings.py        配置（读 .env）
├── app/tracing.py         OTel → Studio tracing 接入（未配置则零开销）
├── app/tools/             中台工具薄封装 ★ 端点对齐点
│   ├── base.py            httpx 客户端/重试/认证（空 token 不发头）
│   ├── search.py          search_admission_data / search_knowledge
│   └── writing.py         writing_generate / writing_polish
├── app/workflow/          models / loader(YAML) / store(JSON) / engine
├── scripts/
│   ├── mock_midplatform.py    假中台（:9100，开发演示用，真实中台就绪后弃）
│   ├── midplatform_mcp.py     中台 MCP Server（:9200，四大能力暴露为标准 MCP）
│   ├── smoke_llm.py           LLM 直连冒烟（验证 provider/key/模型名）
│   ├── smoke_tools.py         工具层直连冒烟（验证 httpx→中台链路）
│   └── smoke_agent_service.py 形态B E2E 冒烟（凭据→Agent→会话→真模型→SSE）
├── templates/gaokao_volunteer.yaml   流程模板
├── tests/test_workflow.py  引擎测试（stub，不需要 LLM）
├── .env                   真实配置（含 key，已 gitignore，勿泄露/勿提交）
├── .env.example           配置模板
└── requirements.txt
```

**环境变量说明**（.env）：

| 变量                      | 说明                                                                     |
| ----------------------- | ---------------------------------------------------------------------- |
| AGENTFORGE\_PROVIDER    | `doubao`（当前）或 `dashscope`                                              |
| AGENTFORGE\_MODEL       | 当前 `doubao-seed-2-1-turbo-260628`（注意：该账号无 flash 变体，2.1 系列只有 pro/turbo） |
| ARK\_API\_KEY           | 火山方舟 key（已配好，随 rsync 传输；**保密，勿提交勿外传**）                                 |
| ARK\_BASE\_URL          | `https://ark.cn-beijing.volces.com/api/v3`                             |
| MIDPLATFORM\_BASE\_URL  | 中台地址。本地演示 `http://127.0.0.1:9000`（假中台）；真实中台就绪后改内网地址                    |
| MIDPLATFORM\_TOKEN      | 中台 service token（可空，空则不带认证头）                                           |
| AGENTFORGE\_STORE       | 工单 JSON 存储路径                                                           |
| AGENTFORGE\_STUDIO\_URL | Studio tracing 地址（`http://localhost:3000` 或留空关闭）                       |
| AGENTFORGE\_FAKE\_LLM   | `1`=跳过大模型（无 key 冒烟用），正常部署留空                                            |

***

## 三、目标服务器情况（已勘察，2026-09-02）

| 项      | 值                                                | 判断                            |
| ------ | ------------------------------------------------ | ----------------------------- |
| 系统     | Huawei Cloud EulerOS 2.0 (x86\_64)，root，已装 Conda | ✅                             |
| CPU    | 8 核                                              | ✅ 富余                          |
| 内存     | 15G 总量 / 5.2G available                          | ✅ 富余（全家桶 1.5G 内）              |
| **磁盘** | **40G 已用 94%，仅剩 2.6G**                           | ❌ **部署前必须清理，目标清出 ≥10G**       |
| 负载     | 0.38                                             | ✅ 空闲                          |
| 已有内容   | \~/zhaohongyu/（含 langchain 项目）、conda base        | 部署到 \~/zhaohongyu/agentforge/ |

***

## 四、部署步骤

### 步骤 0：磁盘清理（必做，先看谁在占）

```bash
du -xh --max-depth=2 / 2>/dev/null | sort -h | tail -15   # 摸底
docker system df && docker system prune -a                 # Docker（如有）
conda clean --all -y                                       # Conda 缓存，常有 1-3G
journalctl --vacuum-size=100M                              # 系统日志（已运行98天）
pip cache purge                                             # pip 缓存
df -h /                                                     # 复查，目标 Avail ≥ 10G
```

### 步骤 1：从开发机同步代码（在 Mac 上执行）

```bash
cd "/Volumes/WD_BLACK/project/personal/智能体平台"
rsync -av --exclude .venv --exclude .studio-data --exclude data \
      --exclude __pycache__ --exclude "*.pyc" --exclude .pytest_cache \
      agentforge/ root@<服务器IP>:~/zhaohongyu/agentforge/
```

说明：`.env`（含 ARK\_API\_KEY）随代码同步，服务器侧直接可用；**注意不要提交到任何 git 仓库**。

### 步骤 2：建 Python 环境（服务器上）

```bash
# EulerOS 系统 Python 太老，用 Conda 建
conda create -n agentforge python=3.12 -y
conda activate agentforge

cd ~/zhaohongyu/agentforge
pip install -r requirements.txt -i https://repo.huaweicloud.com/repository/pypi/simple
```

### 步骤 3：验证（三层递进，每层过了再下一层）

```bash
# 3.1 引擎单测（不需要 LLM/网络）
python -m pytest tests/ -q        # 期望：10 passed

# 3.2 LLM 直连冒烟（验证 key/模型名/网络可达方舟）
python -m scripts.smoke_llm       # 期望：provider=doubao ... 回复: 成功

# 3.3 工具链路（起假中台再冒烟）
python -m scripts.mock_midplatform &        # :9000
python -m scripts.smoke_tools               # 期望：3 个工具返回 JSON
```

### 步骤 4：启动工单服务并 E2E

```bash
# 起（前台先试跑；.env 已随代码同步，无需改）
python -m uvicorn app.main:app --host 0.0.0.0 --port 8100

# 验证
curl localhost:8100/templates          # 期望返回模板 JSON

# E2E：创建任务 → 自动检索 → 挂起在人工环节（真模型，约 30-60s）
curl -X POST localhost:8100/tasks -H 'Content-Type: application/json' -d '{
  "template": "gaokao_volunteer",
  "variables": {"province":"河南省","subject_type":"物理类","score":621,
                "preference":"计算机相关，倾向北京上海"}}'

# 查任务（替换 task_id）
curl localhost:8100/tasks/<task_id>

# 提交人工环节 → 自动续跑报告 → finished（报告环节约 3-4 分钟，耐心轮询）
curl -X POST localhost:8100/tasks/<task_id>/submit -H 'Content-Type: application/json' -d '{
  "step_id":"confirm_profile","output":"画像确认无误","actor":"counselor"}'
```

> 注意：submit 是同步执行，报告生成 3-4 分钟会导致 curl 超时——**正常现象**，服务端照常跑完，轮询 `GET /tasks/<id>` 看 finished。异步化在 W3。

**API 一览**：

| 端点                            | 用途                                          |
| ----------------------------- | ------------------------------------------- |
| GET /templates                | 模板列表                                        |
| POST /tasks                   | 创建任务（template + variables）                  |
| GET /tasks?status=\&assignee= | 任务列表 / 待办收件箱                                |
| GET /tasks/{id}               | 任务详情（history/current/planned/interventions） |
| POST /tasks/{id}/submit       | 人工环节提交                                      |
| POST /tasks/{id}/skip         | 跳过环节                                        |
| POST /tasks/{id}/redo         | 重做（失败当前环节直接重跑；已完成环节插副本）                     |
| POST /tasks/{id}/reassign     | 人工环节改派                                      |

### 步骤 5：systemd 常驻

```bash
# 先确认 conda 环境路径
conda env list    # 记下 agentforge 环境的 python 绝对路径，如 /root/miniconda3/envs/agentforge/bin/python

cat > /etc/systemd/system/agentforge.service <<'EOF'
[Unit]
Description=AgentForge Workorder Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/zhaohongyu/agentforge
EnvironmentFile=/root/zhaohongyu/agentforge/.env
ExecStart=/root/miniconda3/envs/agentforge/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8100 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now agentforge
systemctl status agentforge          # 期望 active (running)
journalctl -u agentforge -f          # 看日志
```

> `--workers 2`：8C15G 的机器可以开 2-4 个 worker；注意 JSON 存储是多进程共享文件，目前单文件无锁——**并发写入有覆盖风险，正式多 worker 前把 store 换 SQLite/PG（W3 项）**。稳妥起见初期 `--workers 1`。

### 步骤 6（可选）：AgentScope Studio

```bash
# EulerOS 源里没有 Node≥20，用官方二进制
cd /usr/local
wget https://nodejs.org/dist/v20.19.0/node-v20.19.0-linux-x64.tar.xz
tar xf node-v20.19.0-linux-x64.tar.xz && mv node-v20.19.0-linux-x64 node
ln -sf /usr/local/node/bin/node /usr/local/bin/node
ln -sf /usr/local/node/bin/npm /usr/local/bin/npm
ln -sf /usr/local/node/bin/npx /usr/local/bin/npx

npm install -g @agentscope/studio
as_studio    # :3000，数据落 $HOME/.AgentScope-Studio（Linux 无 macOS 沙箱问题）
```

`.env` 中 `AGENTFORGE_STUDIO_URL=http://localhost:3000`，重启 agentforge 后每个自动环节自动上报 trace。
**省资源替代方案**：服务器不装 Studio，`.env` 该项留空（零开销），需要排查时在开发机本地起 Studio。

### 步骤 7：官方 Web UI（agent-service，形态 B）★ 推荐的主入口

> AgentScope Studio（独立 npm 包）**已停止维护并归档**；AgentScope 2.0 官方 Web UI 内置于 agent-service，覆盖 MCP/Skill Hub 管理、Agent Team 编排、多会话聊天、人工干预、知识库、定时任务。

```bash
# 7.1 Redis（agent-service 的存储后端）
docker run -d --name agentforge-redis --restart unless-stopped -p 6379:6379 redis:7

# 7.2 中台 MCP Server（把四大能力暴露为标准 MCP，:9200）
systemctl enable --now agentforge-mcp     # 见下方 unit 文件

# 7.3 agent-service + Web UI（:8300）
systemctl enable --now agentforge-svc
```

systemd unit（`/etc/systemd/system/agentforge-mcp.service`、`agentforge-svc.service`）：

```ini
# agentforge-mcp.service
[Unit]
Description=AgentForge midplatform MCP server (:9200)
After=network.target agentforge-mock.service
[Service]
WorkingDirectory=/home/zhaohongyu/AgentScope/agentforge
EnvironmentFile=/home/zhaohongyu/AgentScope/agentforge/.env
ExecStart=/root/miniconda3/envs/agentforge/bin/python -m scripts.midplatform_mcp
Restart=always
[Install]
WantedBy=multi-user.target

# agentforge-svc.service
[Unit]
Description=AgentForge agent-service (Web UI :8300)
After=network.target
Wants=agentforge-mcp.service
[Service]
WorkingDirectory=/home/zhaohongyu/AgentScope/agentforge
EnvironmentFile=/home/zhaohongyu/AgentScope/agentforge/.env
ExecStart=/root/miniconda3/envs/agentforge/bin/python agent_service_app.py
Restart=always
[Install]
WantedBy=multi-user.target
```

Web UI 构建产物在 `webui/`（gitignore，不入库）。**重建方法**：

```bash
git clone --depth 1 https://github.com/agentscope-ai/agentscope.git /tmp/agentscope-src
cd /tmp/agentscope-src/examples/web_ui/frontend
npm install && npm run build
cp -r dist /home/zhaohongyu/AgentScope/agentforge/webui
```

**首次使用**（浏览器打开 `http://<服务器>:8300`）：

1. **登录**：会自动跳到 `/login`。账号在服务器 `data/users/` 文件夹维护（见下方"用户管理"），初始账号 `admin / admin123`
2. 登录后进入官方 UI，「凭据」页 → 新建 → **豆包 ARK**（自定义凭据类型 `app/ark_credential.py`）→ 填 ARK key 即可（base_url 已内置）
3. 「聊天」页 → 新建 Agent/会话 → 模型下拉会显示 ARK 真实模型（doubao-seed / deepseek / kimi / glm 等）→ 选 `doubao-seed-2-1-turbo-260628` 开聊
4. 工作区默认已挂中台 MCP（检索/写作 4 工具），模型可自主调用
5. 「MCP」「Skill」「知识库」「定时任务」等页对应各项管理能力

> 注：不要用「OpenAI API」类型接 ARK——其模型下拉是 OpenAI 静态目录（GPT 系列），选了无法调用。「豆包 ARK」类型协议相同，但模型目录是 ARK 真实模型。
>
> **模型列表心跳同步**：服务启动时及此后每 24 小时，自动用 `.env` 里的 `ARK_API_KEY` 调 `GET /api/v3/models` 拉取账号真实可用模型并刷新模型卡（自动过滤图像/视频/向量/语音等非对话模型，只保留对话家族 doubao/deepseek/kimi/glm/qwen/mistral）。同步失败则保留现有卡片，不影响服务。已知模型的上下文/输出规格写在 `app/ark_credential.py` 的 `_CHAT_MODELS`（新模型自动用保守默认值 128k/16k）。

**用户管理**（文件驱动，无注册）：

```bash
# 增加用户：在 data/users/ 下建 <用户名>.txt，内容为明文密码
echo 'mypassword' > data/users/zhangsan.txt

# 改密码：直接编辑对应文件（下次登录生效，已登录会话最长再保持 7 天）
# 删除用户：删文件即可（其历史数据仍在 Redis，按用户名隔离）
```

- 用户名规则：2-32 位字母/数字/下划线/连字符
- 会话：Redis 存储，7 天滑动过期；`/login` 页可退出登录
- 安全：未登录一律 401（API）/ 302 跳登录页；客户端伪造 `X-User-ID` 头无效（身份以服务端会话为准）
- `data/` 已 gitignore，密码文件不会入库

**E2E 冒烟**：`python scripts/smoke_agent_service.py`（登录→建凭据→Agent→会话→真模型对话→SSE 收流→清理，期望输出 PASS）

### 步骤 8（可选）：外网访问

华为云控制台 → 安全组 → 放行 8100（如需公网访问）。**建议仅内网/VPN 访问**；该服务无鉴权，公网裸奔有风险，上公网前需加网关鉴权。

***

## 五、已知坑（全部踩过，直接绕开）

| # | 坑                                     | 解法                                                                                                                                                            | <br />                             |
| - | ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | :--------------------------------- |
| 1 | 磁盘 94%                                | 步骤 0 先清理，清不够就别装                                                                                                                                               | <br />                             |
| 2 | EulerOS 系统 Python 老                   | 一律用 conda 环境                                                                                                                                                  | <br />                             |
| 3 | 模型名                                   | 用 `doubao-seed-2-1-turbo-260628`；`flash` 在该账号不存在；改模型名可先 \`curl -s <https://ark.cn-beijing.volces.com/api/v3/models> -H "Authorization: Bearer $ARK\_API\_KEY" | grep -o '"id":"\[^"]\*"'\` 查账号可用列表 |
| 4 | MIDPLATFORM\_TOKEN 为空时发非法头 `Bearer `  | 已修复（代码里空 token 不带认证头），知道即可                                                                                                                                    | <br />                             |
| 5 | submit 同步等 3-4 分钟 curl 超时             | 正常，轮询任务状态；不是 bug                                                                                                                                              | <br />                             |
| 6 | `.env` 必须在服务启动前就位                     | settings 用 dotenv 加载，运行中改不生效，改完要重启                                                                                                                            | <br />                             |
| 7 | Linux 沙箱无 PYTHONHOME 问题               | Mac TRAE 环境特有，Linux 不涉及，`python` 直接用                                                                                                                          | <br />                             |
| 8 | Studio Linux 数据目录                     | `$HOME/.AgentScope-Studio`，正常权限即可，无需 HOME 重定向                                                                                                                 | <br />                             |

***

## 六、后续任务路线（W1 已收口）

| 阶段     | 内容                                                                                        | 状态        |
| ------ | ----------------------------------------------------------------------------------------- | --------- |
| W1     | 工单引擎 + 检索/写作工具 + 高考志愿 E2E + 按钮式干预 + 真实 LLM + Studio tracing                               | ✅ 完成      |
| **待办** | 中台真实端点对齐：`app/tools/*.py` 各工具类的 `endpoint` 常量 + MIDPLATFORM\_BASE\_URL/MIDPLATFORM\_TOKEN | ⏳ 等中台接口信息 |
| W2     | 可视化/标注工具接入、长期记忆（ReMe/Mem0）、表单式干预（planned 增删换序 API）                                        | 未开始       |
| W3     | 异步长任务（submit 立即返回）、Redis 精确缓存、存储换 SQLite/PG、多 worker                                      | 未开始       |
| 之后     | 工单控制台前端单页、拖拽小图编辑器（2-4 周）、RBAC、agent\_service 生产化                                          | 未开始       |

**对接真实中台时唯一要改的**：每个工具类的 `endpoint` 字符串（如 `/search/query` → 中台真实路由）；工具 description 已按"何时该用/何时不该用"打磨，新工具保持此写法——它直接决定模型调用准确率。

***

## 七、参考

- 完整决策与验证记录：`trae/2026-09-02_AgentScope选型与提速分析.md`（开发机项目根目录）

- AgentScope 文档：<https://docs.agentscope.io/versions/2.0.4/zh>

- AgentScope Studio：<https://github.com/agentscope-ai/agentscope-studio>

- 需求文档原件：项目根目录"工具"文件夹下《项目需求文档》（见 trae 文档第一节引用）

