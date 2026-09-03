# AgentForge 交接与部署文档

> 生成时间：2026-09-02。本文档自包含全部背景、现状、部署步骤与已知坑，供在服务器侧独立执行，无需额外上下文。

***

## 一、项目背景（是什么、为什么）

**项目**：智汇平台——基于开源框架 AgentScope 2.0 的智能体执行平台。
**核心形态**：AgentScope 官方 agent-service 多租户平台（Web UI :30000）——MCP/Skill Hub、Agent Team（大A/小A）编排、多会话聊天、知识库、定时任务、版本封板。早期工单式（BPM）演示形态已于 2026-09-03 下线删除。

**关键决策记录**（详见本地 trae/2026-09-02\_AgentScope选型与提速分析.md）：

| 决策点    | 结论                                                |
| ------ | ------------------------------------------------- |
| 框架     | AgentScope 2.0（Python），不用自研运行时                    |
| 四大能力服务 | 用户已有：内网 FastAPI 中台（检索/标注/可视化/写作），本服务仅做工具薄封装对接     |
| 流程定义   | YAML 声明式模板（拖拽画布后置，非 MVP 范围）                       |
| 人工干预   | 按钮式（submit/skip/redo/reassign）已实现；表单式（W2）、拖拽式（后置） |
| 大模型    | 火山方舟豆包（OpenAI 兼容协议），远端 API，**无本地推理，不需要 GPU**      |

**架构**：

```
Web UI（webui-src/ → webui/，:30000）
       │ HTTP/SSE
       ▼
agent-service（agent_service_app.py，多租户，Redis 存储）
       │
       ▼
AgentScope Agent（大A 主理人 / 小A 成员团队编排 + 版本封板 + MCP/Skill/知识库）
```

**当前状态**：

- AgentScope Studio tracing 接入验证 ✅（可选组件）
- 早期工单引擎（W1 演示形态，:8200）已于 2026-09-03 删除
- **官方 Web UI 上线**（agent-service :30000，MCP/Skill Hub、Agent Team、多会话、人工干预、知识库、定时任务）✅ E2E PASS

***

## 二、代码结构

```
agentforge/
├── agent_service_app.py   AgentScope 官方 agent-service + Web UI（:30000，需 Redis）★ 唯一入口
├── app/agent_factory.py   构建 Agent（provider 分支 doubao|dashscope + TracingMiddleware）
├── app/agent_runner.py    自动环节执行器（上下文注入 + fake-llm 开关）
├── app/settings.py        配置（读 .env）
├── app/tracing.py         OTel → Studio tracing 接入（未配置则零开销）
├── app/tools/             中台工具薄封装 ★ 端点对齐点
│   ├── base.py            httpx 客户端/重试/认证（空 token 不发头）
│   ├── search.py          search_admission_data / search_knowledge
│   └── writing.py         writing_generate / writing_polish
├── scripts/
│   ├── smoke_llm.py           LLM 直连冒烟（验证 provider/key/模型名）
│   └── smoke_agent_service.py E2E 冒烟（凭据→Agent→会话→真模型→SSE）
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

### 步骤 3：验证（两层递进，每层过了再下一层）

```bash
# 3.1 全量回归（不需要真实 LLM/网络）
python -m pytest tests/ -q

# 3.2 LLM 直连冒烟（验证 key/模型名/网络可达方舟）
python -m scripts.smoke_llm       # 期望：provider=doubao ... 回复: 成功
```

> 旧工单服务（app/main.py，:8200/:8100）及其 systemd unit `agentforge.service` 已于 2026-09-03
> 彻底删除；主服务入口见步骤 5（agentforge-svc，:30000）。

### 步骤 4（可选）：AgentScope Studio

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

`.env` 中 `AGENTFORGE_STUDIO_URL=http://localhost:3000`，重启 agentforge-svc 后自动上报 trace。
**省资源替代方案**：服务器不装 Studio，`.env` 该项留空（零开销），需要排查时在开发机本地起 Studio。

### 步骤 5：官方 Web UI（agent-service）★ 主入口

> AgentScope Studio（独立 npm 包）**已停止维护并归档**；AgentScope 2.0 官方 Web UI 内置于 agent-service，覆盖 MCP/Skill Hub 管理、Agent Team 编排、多会话聊天、人工干预、知识库、定时任务。

```bash
# 7.1 Redis（agent-service 的存储后端）
docker run -d --name agentforge-redis --restart unless-stopped -p 6379:6379 redis:7


# 7.2 agent-service + Web UI（:30000）
systemctl enable --now agentforge-svc
```

systemd unit（`/etc/systemd/system/agentforge-svc.service`）：

```ini
# agentforge-svc.service
[Unit]
Description=AgentForge agent-service (Web UI :30000)
After=network.target
[Service]
WorkingDirectory=/home/zhaohongyu/AgentScope/agentforge
EnvironmentFile=/home/zhaohongyu/AgentScope/agentforge/.env
ExecStart=/root/miniconda3/envs/agentforge/bin/python agent_service_app.py
Restart=always
[Install]
WantedBy=multi-user.target
```

Web UI **源码已入库 `webui-src/`**（基于官方 examples/web_ui 的定制版：提示词模板下拉 +
大A/小A 类型徽章分组 + 主理人团队成员选择器/AI 推荐 + 团队互动流程图 + 任务归档对话框 +
agent 版本封板区；原独立 git clone + patch 的方式已废弃）。构建产物 `webui/`（gitignore，
不入库）。**重建方法**：

```bash
bash scripts/build_webui.sh   # npm install（首跑）+ 构建 + 部署到 webui/
```

跟踪官方上游变更时，从 https://github.com/agentscope-ai/agentscope 的
`examples/web_ui/frontend` 对比 `webui-src/` 手工合并（参考
`tests/test_webui_static.py` 的定制点回归锁）。

**首次使用**（浏览器打开 `http://<服务器>:30000`）：

1. **登录**：会自动跳到 `/login`。账号在服务器 `data/users/` 文件夹维护（见下方"用户管理"），初始账号 `admin / admin123`
2. 登录后进入官方 UI，「凭据」页 → 新建 → **豆包 ARK**（自定义凭据类型 `app/ark_credential.py`）→ 填 ARK key 即可（base_url 已内置）
3. 「聊天」页 → 新建 Agent/会话 → 系统提示词上方可选**提示词模板**（`prompt_templates/*.yaml`，收集到新模板放入该目录即可）→ 模型下拉会显示 ARK 真实模型（doubao-seed / deepseek / kimi / glm 等）→ 选 `doubao-seed-2-1-turbo-260628` 开聊
4. MCP 不再默认注入——需要时在「MCP」页显式添加（真实中台就绪后接入）
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

### 步骤 6：智能写作 MCP Server（:30110）

注册包在 `mcp_registry/smart_writing.json`（9 工具 → 两个后端 13 个 method，
原稿见 AgentScope/mcp_data/）。实现为 `app/smart_writing.py` 无状态
streamable-http MCP server，systemd 常驻：

```bash
systemctl status agentforge-smart-writing   # 健康检查
curl http://localhost:30110/health          # 应列出 9 个工具
```

**挂载到平台会话**（Web UI 的 MCP 面板添加，或 API）：

```bash
curl -X POST "http://localhost:30000/workspace/mcp?agent_id=<AID>&session_id=<SID>" \
  -H "X-User-ID: admin" -H "Content-Type: application/json" \
  -d '{"name":"smart-writing","is_stateful":true,"mcp_config":{"type":"http_mcp","url":"http://localhost:30110/mcp"}}'
```

**注意：必须 `is_stateful: true`**。官方 app 对 stateless MCPClient 有缓存
bug（第二轮对话起 setup 失败），详见 TESTS.md 第 21 节。后端地址
（140.210.4.206:30010 搜索类 / 116.204.102.229:30020 其余）变更时改
`mcp_registry/smart_writing.json` 重启服务即可。

### 步骤 7（可选）：外网访问

华为云控制台 → 安全组 → 放行 30000（如需公网访问）。**建议仅内网/VPN 访问**；上公网前需加网关鉴权。

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

