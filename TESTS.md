# 测试用例说明（TESTS.md）

自动化测试体系：**534 个用例**（532 通过 + 2 冒烟跳过），`pytest` 一条命令全量回归（约 22 秒，不依赖任何真实外部服务）。

> 2026-09-03：旧工单服务（`app/main.py`、`app/console.py`、`app/workflow/`）及其测试
> （test_main_api / test_console_api / test_workflow，共 55 例）随服务下线一并删除。

## 快速使用

```bash
# 全量回归（日常开发必跑）
python -m pytest

# 只跑单个模块
python -m pytest tests/test_auth_api.py -v

# 按关键词筛选
python -m pytest -k "locked" -v

# 需要真实外部依赖的冒烟测试（ARK key / 真模型 / Redis）
python -m pytest -m smoke          # 默认不跑
bash scripts/smoke_agent_service.py  # 真模型 E2E 冒烟（形态 B）
```

依赖：`pytest fakeredis pytest-asyncio`（环境内已装）。

## 测试隔离原则

| 真实依赖 | 测试中的替身 |
|---|---|
| Redis（会话存储） | `fakeredis.FakeAsyncRedis` |
| 用户目录 `data/users/` | `tmp_path` 临时目录 |
| ARK `/v3/models` 接口 | 假 `httpx.AsyncClient`（可注入状态码/payload/异常） |
| 中台 HTTP / MCP Server | `httpx.MockTransport` |
| LLM 调用 | fake runner / `fake_llm` 模式 |
| 文件持久化 | `tmp_path`，每用例独立 |
| 官方 agent-service | **`tests/official_contract.py` 工厂函数**（真实契约快照，禁止内联手写响应结构） |

## 用例清单

### 1. `test_auth_unit.py` — 鉴权单元（14 例）
| 组 | 覆盖点 |
|---|---|
| TestCheckPassword | 正确/错误/不存在用户；文件尾空白 strip；**输入端不 strip（精确匹配）**；空密码文件拒绝；含内部空格密码；**中文密码**（曾发现 `compare_digest` 非 ASCII 崩溃 bug，已修）；非法用户名 11 种（路径穿越 `../`、中文、过长、带扩展名等）；合法用户名形状 5 种 |
| TestSession | 会话写入 Redis 且带 TTL；20 次 token 不重复 |

### 2. `test_auth_api.py` — 鉴权 API + 中间件全链路（44 例）

| 组 | 覆盖点 |
|---|---|
| TestLoginApi | 登录 200 + cookie；cookie 安全属性（httponly/samesite/path）；错误密码 401；未知用户 401；空 body 422；/auth/me 登录前后 |
| TestMiddlewareAccess | 未登录 API 401；POST 401；页面 307 跳 /login；/index.html 307；登录页公开；/auth/* 放行；/health 探活放行；/assets/* 静态放行 |
| TestIdentityInjection | **伪造 X-User-ID 被会话覆盖**（安全核心）；仅伪造头无 cookie → 401；正常请求携带身份；垃圾 cookie → 401 |
| TestLogoutAndSession | 登出即失效；会话跨请求保持；**TTL 滑动续期**；过期会话拒绝 |
| TestMultiUserIsolation | 双用户身份切换独立；双会话互不干扰 |
| TestPasswordFileHotReload | 改密码文件下次登录生效；删用户文件立即封禁 |
| 生产路径抽查 | 6 条生产路由未登录全部 401 |

### 3. `test_ark_credential.py` — ARK 凭据与模型同步（38 例）

| 组 | 覆盖点 |
|---|---|
| TestChatModelFilter | 接受 10 种对话家族 ID（doubao/deepseek/kimi/glm/qwen/mistral/翻译）；拒绝 16 种非对话 ID（向量/视频/图像/3D/编辑/浏览版/预训练/路由/UI 智能体/未知家族） |
| TestWriteChatCards | 已知模型用人工核对规格（256k/32k）；未知模型保守默认（128k/16k）；**重写清除下架旧卡**；embedding 卡格式 |
| TestSyncArkModels | 同步成功（过滤 embedding + 未知家族）+ 认证头/URL 正确；无 key 返回 0 不动卡片；500 失败保留现有卡片；网络异常保留卡片；从环境变量取 key |
| TestArkCredentialSchema | type/base_url 默认值；UI 标题「豆包 ARK」；模型类绑定；无 TTS |

### 4. `test_registry.py` — 注册中心（20 例）

| 组 | 覆盖点 |
|---|---|
| TestMcpServers | 增查；**list 不泄漏 token（get 内部可见）**；删除幂等；只更新已存在；重复注册覆盖 |
| TestAgentLifecycle | 创建默认 draft；按状态过滤；缺失返回 None；draft 可编辑；**locked/published 编辑抛 PermissionError**；完整状态机 draft→locked→published |
| TestWorkspace | 空工作空间自动创建；消息追加顺序与时间戳；清空；**多 Agent 工作空间隔离** |
| TestPersistence | 重启（重建 Registry）后 MCP/Agent/消息完整恢复；父目录不存在时自动创建且产物是合法 JSON |

### 5. `test_tools.py` — 中台工具层（13 例）

| 组 | 覆盖点 |
|---|---|
| TestPost | 成功；payload 原样 JSON 传递；**500 后重试成功**；重试耗尽（次数 = RETRIES+1）+ RuntimeError；连接错误同样重试；4xx 也走重试路径 |
| TestToChunk | JSON 序列化；中文不转义；**超长截断到 8000** |
| TestWritingTools | 端点绑定 `/writing/generate`；input_schema 必填字段；元数据；call 全链路（含 mock 中台回包） |

### 6. `test_mcp_client.py` — MCP 客户端协议（13 例）

| 组 | 覆盖点 |
|---|---|
| TestParseResponse | 纯 JSON；SSE 流（跳过无 id 通知）；SSE 无 id 报 McpError |
| TestRpc | JSON-RPC 信封（jsonrpc/method/id）+ Accept 头；HTTP 500 抛错；JSON-RPC error 抛错；Bearer token 注入 |
| TestConnect | initialize 握手 + initialized 通知 + session id 传递；无 session 则不发通知 |
| TestListTools | 全流程 connect→tools/list；字段映射；缺 schema/description 容错 |
| TestCallTool | 多文本块拼接、非文本块忽略；工具错误抛 McpError；空结果返回空串 |

### 7. `test_startup_hook.py` — ASGI lifespan 钩子（5 例）

startup complete 触发回调；**重复 complete 仅触发一次**；startup failed 不触发；shutdown 不触发；普通 HTTP scope 透传无注入。

### 8. `test_prompt_templates.py` — 提示词模板（21 例）

| 组 | 覆盖点 |
|---|---|
| TestListPromptTemplates | 按文件名排序；字段完整；description 默认空；**缺 name 回退文件名**；**坏文件只跳过不影响其余**（语法错误/缺 content）；目录不存在返回空；**环境变量覆盖目录**；仓库种子模板结构合法且名称唯一 |
| TestPromptTemplatesApi | GET /prompt-templates 返回模板列表；空目录返回空列表 |
| TestSchemaMiddleware | **注入 system_prompt.prompt_templates 且保留原属性**；**content-length 与改写后 body 一致**；分片响应重组；其他路径透传；**非 GET 透传不注入**；模板为空保持官方 schema；非 200 透传；非 JSON 透传；**坏 JSON body 原样返回不抛异常**；缺目标字段原样返回；lifespan 等非 http scope 透传 |

### 9. `test_spa_static.py` — SPA 深链接回退（8 例）

| 覆盖点 |
|---|
| 根路径 / 真实静态文件正常服务；**深链接（/chat/<agent>/<session>）浏览器刷新回退 index.html**；多级深链接回退；**API 客户端（json Accept）404 不被 HTML 掩盖**；无 Accept 头保持 404；目录无 index.html 时 fallback 自身 404 不抛异常；路径穿越不泄漏敏感文件 |

### 10. `test_agent_type.py` — 大A/小A 类型（27 例）

| 组 | 覆盖点 |
|---|---|
| TestStore | 默认 member；set/get；**非法值抛错**；删除；删除不存在 no-op；**同值跳过写盘**；损坏文件降级空；**文件中非法值过滤**；文件缺失空；环境变量路径 |
| TestHelpers | **_match 集合路径（含尾斜杠）/条目路径/拒绝场景**（多级、前缀不同、集合 DELETE）；_extract 剥离/无键/非 JSON；schema 属性形状 |
| TestMiddlewareWrite | **POST 剥离透传 + 响应拿 id 存映射**；POST 无 agent_type 不写文件；PATCH 按路径存；**PATCH 非法值忽略**；DELETE 清理；DELETE 不存在的 agent；**POST 无 agent_type 透传不挂起**（body 消费后必须重建 receive） |
| TestMiddlewareRead | 列表注入 leader/member；空列表；非 agent 路径透传；lifespan 等非 http scope 透传 |

### 11. `test_leader_team.py` — 主理人预置团队成员 + AI 推荐（29 例）

| 组 | 覆盖点 |
|---|---|
| TestStore | 映射文件缺失空；set/get/delete；**同值去重 + 空列表不写盘**；损坏 JSON 降级空 |
| TestSection | build_team_section 提示词格式（成员清单注入）；strip 剥离旧段落幂等 |
| TestExtract | 对象数组（id/name/description）；纯 id 数组；无键/非 JSON/非列表返回 None |
| TestMiddleware | **POST leader 剥离 team_members + 注入提示词 + 写 sidecar**；无 team_members 透传；**member 携带 team_members 被忽略**；**leader 不能当成员（422）**；GET 注入 team_members；PATCH 重写段落；PATCH 空列表清空；DELETE 清理 sidecar；非 agent 路径透传 |
| TestRecommend | 未登录 401；空上下文 422；**无候选返回空**；LLM 成功返回推荐+理由；**LLM 失败降级空推荐不 5xx**；候选排除 leader 自身与其他 leader |
| TestLLMUtils | extract_json：裸 JSON / ```json 围栏 / 文本内嵌 / 非法返回 None |

### 12. `test_team_archive.py` — 任务归档（封档）（16 例）

| 组 | 覆盖点 |
|---|---|
| TestStore | 封档文件缺失空；add/get（按时间倒序）；损坏 JSON 降级空 |
| TestTranscript | 会话转写（文本 + tool_call 摘要）；空会话；**超长截断** |
| TestSummarize | **LLM 成功生成总结+工作流步骤+新 agent 草案**；**LLM 失败降级基础草稿**（不 5xx）；会话不可读降级；空会话降级；未登录 401 |
| TestArchive | 创建封档 + GET 列表；**封档时新 agent 自动注册入库**（agent_type=member）；空名 422；不存在 404；**注册失败不阻断封档**（非致命） |

### 13. `test_webui_static.py` — webui 产物与前端定制补丁回归（8 例）

> 来源事故：曾把前端 `getBaseUrl()` 改成返回空字符串，`new URL(path, '')` 运行时抛
> "Invalid URL"，**前端全部 API 请求失败**（历史会话/凭据/表单 schema 全加载不出，
> 页面却显示"正常"的空状态）。该 bug 在 TS 编译期与后端 pytest 均不可见，故从两个
> 可静态断言的面锁住回归：

| 组 | 覆盖点 |
|---|---|
| TestDeployedWebui | **index.html 存在**（否则静态挂载静默降级）；**引用的本地资源全部存在**（防 cp 中断的半截部署）；**无 /@vite/client 残留**（防误部署 dev 构建） |
| TestPatchSameOrigin | **补丁含 `getBaseUrl = () => window.location.origin`**；**空字符串基址出现即失败**（事故根因回归锁）；**401 → /login 跳转存在** |
| TestPatchNoSetupGate | **setupComplete 门禁移除**（防"连接到服务器"设置页回归）；**/setup 路由重定向 /chat** |

### 14. `test_official_contract.py` — 官方 API 契约快照（9 例）

> 背景：三处中间件/router 解析官方 `POST /agent/` 响应取 id 时只找 `id`/`agent.id`，
> 而真实响应顶层是 `agent_id`，导致创建 leader 类型落回 member、预置成员名单
> 未存储、封档新成员 id 丢失——三个 bug 同根因，且当时 mock 结构失真测试全绿。

`tests/official_contract.py` 把真实响应结构固化为**单一事实源**，全部模拟官方
API 的 mock 必须经其工厂函数构造（禁止测试内联手写结构）。本文件用快照断言
锁死契约键列表，官方版本升级时最先转红，强制重新录制契约并同步修实现。

### 15. `test_llm_utils.py` — LLM 直连工具（8 例）

| 组 | 覆盖点 |
|---|---|
| TestExtractJson | 纯对象/数组；```json 围栏（带/不带语言标记）；前后杂文包裹（对象/数组）；字符串内花括号不破坏平衡；非法输入报错；**数组包对象取最外层**（曾因先扫 `{` 返回内层对象，丢数组语义——推荐成员场景会只剩 1 个，已修） |
| TestLlmChat | MockTransport 模拟 ARK：请求契约（URL/Authorization/messages 顺序）；缺 key 报错；`llm_chat_json` 全链路（ARK 响应 → 围栏文本 → dict） |

### 16. `test_leader_team.py` 新增 `TestCreateLeaderEndToEnd`（1 例）

用户完整使用逻辑：建成员 → 建主理人（带成员）→ GET 回读同时验证
类型 + 成员名单 + 提示词注入 → 编辑换名单（段落重写）。任何一层断裂
（各层单测全绿、组合链路断裂的事故形态）都会在此暴露。

### 17. `test_stack_integration.py` — 生产中间件栈整链（10 例）

复刻 `agent_service_app.py` 的真实包装顺序（Auth→PromptTemplate→LeaderTeam→
AgentType），跑用户真实使用序列：鉴权门禁（业务端点未登录全 401）、
带成员建主理人三件事回读、**recommend-members 端点不被路径匹配吞掉**、
schema/v2 双注入不冲突、封档 API 鉴权。

### 18. 冒烟修复的回归锁（真实环境全旅程发现）

上线前用真实服务（真 Redis + 真 ARK LLM）走完整用户旅程，抓出两个
单测无法发现的 bug，均已按"先失败用例→修→转绿"处理：

| bug | 根因 | 回归锁 |
|---|---|---|
| 归档总结必报"会话不可读 422" | 官方 messages 端点 `limit` 硬上限 200，代码传 500 | fake_call 参数契约校验（断言 limit≤200）+ `_fetch_all_messages` 翻页聚合 2 例（450 条 3 页、before 排他游标语义经真服务实测） |
| 归档总结必 ReadTimeout 且错误信息为空 | 思考型模型对长分析任务实测 30~120 秒，60 秒超时不够 | `TIMEOUT≥180` 回归锁 + 环境变量覆盖用例；异常日志改 `%r`（超时类 str 为空） |

**教训**：mock 不校验参数约束（如 limit 上限）与不计时，是单测假绿的
另两种形态；上线前真实环境冒烟不可省略。

### 19. `test_leader_team.py` 新增 `TestLeaderDefaultPrompt`（5 例）+ 注入格式强化

> 背景（2026-09-03 用户实测反馈）：创建 leader 未写提示词时官方默认提交
> "You're a helpful assistant."，导致 ① hello 回复普通助手、② 模型无团队
> 纪律自行中途 TeamDelete 解散团队（打断未汇报的成员）。同时成员注入段
> 落缺 @id 与职责时，模型只能邀请无名"成员"。

- 默认模板替换：默认英文提示词/空 → 主理人模板（含团队纪律）；
  自定义提示词不动；member 不替换；不带 team_members 键的透传路径同样生效
- 注入段落强化：`- 名字（@id8）：职责` 格式 + "不要提前解散团队"纪律

**教训**：前端 EditAgentDialog 曾回退 `id.slice(0,8)` 作为成员名，注入后
提示词只剩裸 id——名字/职责类展示数据禁止用 id 截断兜底，必须查真实档案。

### 20. `test_agent_version.py`（25 例）——agent 版本封板

> 背景（2026-09-03 用户需求"培育 → 封板 → 对外服务"）：agent 可 freeze
> 出版本号，冻结期间自我迭代停止（PATCH 403）；解冻进开放模式可继续迭代，
> 手动保存产生新版本号；恢复历史版本属显式授权操作，冻结中也可执行。

- **存储层**：版本号自增、同内容复用最新版本（冻结→解冻→再冻结不膨胀）、
  快照只保留 AgentData 配置字段（name/system_prompt/context/react/invite，
  运行时元数据不入档）、损坏文件降级、空记录不落盘
- **冻结拦截**：freeze 后 PATCH 403 且配置不被改；**冻结拦截在外层**——
  PATCH 携带 agent_type 也不落库（零副作用）；解冻后 PATCH 恢复 200
- **版本管理**：save-version 不冻结（开放模式存版）；restore 把配置写回
  历史版本且 current_version 跟随；**冻结中 restore 必须可执行**（经
  `_official_app` 直写绕过自家拦截链——显式人工操作即授权）
- **注入与清理**：GET /agent/ 注入 `version` 字段（frozen/current/latest，
  前端据此渲染冻结徽章）；DELETE agent 清理 sidecar；多段子路径
  （/freeze 等）透传不受中间件影响
- **边界**：freeze 不存在的 agent 404、restore 不存在的版本 404、
  未登录 401、不带 body 的 freeze 不 422、恢复写入失败 502

**契约要点**：`_call_official` 用 httpx ASGI transport 指向**未包装**的
官方 app——若误指向包装链，冻结中 restore 会被自家 403 拦死（测试用
同构 fake 复刻此语义）。真实服务冒烟：freeze→403→unfreeze→save v2→
restore v1 全链路已在 :30000 验证通过。

### 21. `test_smart_writing.py`（31 例）——智能写作 MCP Server

> 背景（2026-09-03 用户需求）：把 mcp_data 的智能写作注册包
> （9 工具 → 2 个后端 13 个 method）注册为平台可用 MCP。
> 实现：`app/smart_writing.py` 无状态 streamable-http MCP server（:30110），
> 注册包入库 `mcp_registry/smart_writing.json`。

- **注册包自检**：9 工具齐全、13 条路由、地址规则（搜索类 :30010 /
  其余 :30020）、description/inputSchema 与注册包逐字一致（文档注意事项 1：
  "何时用/何时不用"负例直接决定 Agent 选工具准确率，禁止压缩改写）
- **路由**：when 条件（mode=natural/boolean、action=filter/cluster/save/
  list/fetch、always）、无匹配抛错
- **信封**：query_fields 过滤（未列入的入参丢弃）、统一
  `{"utterance":{"query":...},"method":...,"source":"vr"}`
- **条件必填**（注意事项 2）：filter_solutions filter 时 query 必填、
  user_results 按 action 要求 name/time/info——JSON Schema 表达不了的
  运行时校验，校验失败**不发任何 HTTP 请求**
- **错误透传**（注意事项 3）：error_msg 原文抛给 Agent（便于自行修正
  参数重试）；HTTP ≥400 透传
- **超时**（注意事项 4）：extract_solutions/filter_solutions ≥300s，
  其余 60s
- **MCP 协议端点**：initialize（协议版本 2025-03-26）、notification 202、
  tools/list 原样透传、tools/call 成功/isError 语义、未知工具/方法报错、
  GET 405（无状态 server 标准行为）

**真实链路验证**（:30000 平台，2026-09-03）：官方 MCPClient 连 :30110
列出 9 工具；`POST /workspace/mcp` 挂载后 agent 对话中真实调用
`mcp__smart-writing__detect_word_ontology`，后端返回本体数据（"提高"→
运动 0.9999），agent 基于结果完成解读。

**坑（重要）**：挂载 MCP 必须用 `is_stateful: true`。官方 app 的
workspace 对 **stateless** MCPClient 有缓存 bug——第一轮对话正常，
第二轮起 setup 失败（"The session could not be prepared"，MCP server
甚至收不到请求）。stateful 挂载 + 无状态 server（不签发 session id）
组合验证稳定。另：HITL confirm 事件重放 POST /chat/ 会触发新 run
setup 失败（疑似官方 bug，UI 内确认未复现，观察中）。

### 22. `test_sync_scientific_skills.py`（7 例）——科学技能包同步

> 背景（2026-09-04 用户需求）：把 K-Dense-AI/scientific-agent-skills
> 的全部技能包补充到服务端技能中心。
> 实现：`scripts/sync_scientific_skills.py` 解析每个技能包 SKILL.md 的
> YAML frontmatter（name/description/metadata.version），生成
> skill.json 卡片后整目录同步进 `skill_registry/`（幂等，源侧删除
> 的文件不残留）；163 个科学技能包全部上架（`/hub/skill/local/cards`）。

- **frontmatter 解析**：顶层标量提取、引号剥壳、metadata.version 识别、
  无 frontmatter 返回空
- **同步产出**：卡片字段（name/description/tags/version/author）、
  SKILL.md 与 references 随目录复制、缺 SKILL.md 或缺 description 的
  坏包跳过
- **幂等与清理**：二轮同步不重复；源里删除的文件在 registry 侧消失
- **与 LocalSkillHub 衔接**：同步产物经 `list_skills` 真实扫描可见
- **真实链路**（:30000）：卡片搜索 q=rdkit 命中；hub install 201；
  `/workspace/skill/from-library` 装配 201

### 23. `test_auth_api.py` 新增深链接跳转用例（2026-09-04 E2E 发现）

> 登出后浏览器直接访问/刷新 /chat、/mcp 等深链接收到裸 401 JSON
> （旧逻辑只对 `/` 做 307）。修复：未登录 GET + Accept 含 text/html
> 一律 307 → /login；API 客户端仍 401 JSON。

- `test_deep_link_pages_redirect_for_browsers`：/chat /mcp /skill
  /credential /knowledge 五个前缀 307 断言
- `test_deep_link_401_for_api_clients`：无 Accept: text/html 的请求
  保持 401 + 中文 detail

**真实链路验证**（:30000，2026-09-04 浏览器 E2E 全 6 步）：
未登录访问 / 307 → /login；登录 → 用户菜单显示当前用户 + 退出登录；
退出 → /login；登出后访问 /chat 被踢回 /login；再登录成功。

### 24. i18n 默认中文（2026-09-04 E2E 发现）

> TeamFlowPanel 曾渲染成英文（"Leader / No role descrip"）：i18n
> LanguageDetector 跟随 navigator，英文系统/无头浏览器首访即英文。
> 修复：`webui-src/src/i18n/index.ts` fallbackLng 'zh'、detection 仅
> localStorage（用户显式切换过才用英文）。浏览器 E2E 复验 SVG 节点
> 全中文（「主理人（大A）」「暂无职责说明」「团队互动」「N 位成员」）。

### 25. `test_webui_static.py` 新增 `TestSrcAssetReloadGuard`（2026-09-04 事故）

> 用户点击技能中心 rdkit 报 "Failed to fetch dynamically imported
> module: …/highlighted-body-KPVGNVTW-BEhgSw5n.js"：部署新构建时
> 整目录删除旧 assets，部署前已在浏览器打开的旧 SPA 懒加载旧 hash
> chunk → 404 → 页面崩溃。no-cache 只保证"下次导航拿新 HTML"，
> 救不了"已打开的旧页面"。双保险修复：

- **部署侧**：`scripts/build_webui.sh` 增量合并（禁止整目录删除），
  旧 hash chunk 保留给旧页面用；另加 node >= 20 版本防护（PATH 里的
  v12 老 node 会让 tsc 报 "Unexpected token '?'"）
- **前端侧**：`main.tsx` 全局监听 unhandledrejection / error（捕获
  阶段），识别动态 import 失败后自动 `location.reload()`（index.html
  是 no-cache，刷新即新版）；sessionStorage 标记防死循环，load 成功
  后清除（下次部署失效仍可再自愈）
- 测试：源码断言 main.tsx 含错误识别+标记+清除三要素；构建脚本
  无 `rm -rf` 且为 mkdir -p + cp 增量模式

**真实浏览器验证**：技能中心点 rdkit 详情正常渲染无错误页；注入不
存在的模块 import → 页面自动 reload（导航类型=reload）；load 后标记
清除 PASS；第二次失败不再刷新（防死循环）。
注意：事故时已打开的旧页面因无自愈代码，需手动强刷一次（Ctrl+F5）。

### 26. 团队面板 = 工作流驾驶舱（2026-09-07 用户原型图重构）

> 用户反馈五大问题：点成员跳走而非看互动；连线计数点击显示
> "邀请加入"；团队名"高考志愿规划小组"不展示；志愿兵汇报全文
> 看不到；无时间轴概念。根因：数据全在消息流里（hint 块
> `<team-message from=…>` 全文、TeamCreate input 的 name/description、
> 块级 created_at），但旧面板只渲染了摘要关系图。

重构（TeamFlowPanel.tsx 完全重写）：
- 头部：团队名 + 运行状态徽章（TeamDelete 前后区分）+ 任务描述
- 统计行：N 位成员 · N 次协作 · N 次工具调用 · 运行 Xs
- 四 Tab：
  - 团队动态：时间轴（HH:MM:SS + 事件 + 可展开全文），支持按成员过滤
  - 工作流：组建 → 分派 → 执行（含被中断状态）→ 汇报 → 汇总
  - 成员：卡片（名字/职责/状态徽章/协作明细），"进入会话迭代"为
    次要入口；点 SVG 成员卡片 = 本页看互动，不再跳转
  - 产物：成员汇报全文 Markdown 渲染
- 事件模型：user_task / team_created / member_joined / dispatch /
  member_report（全文）/ member_interrupted（system-reminder 解析）/
  leader_say / tool / final / team_deleted，全部带块级时间戳

测试：`test_webui_static.py::TestSrcLeaderTeam::test_team_flow_is_workflow_cockpit`
锁定汇报全文解析、四 Tab、本页互动、中断状态、时间戳与 i18n 键。
真实浏览器 E2E（高考主理会话）：头部/Tab/时间轴/成员状态/产物全文/
五阶段工作流全部断言通过。

### 27. `test_webui_static.py` 新增 `TestSrcFocusedLayout`（2026-09-07 布局重构）

> 用户布局诉求：中间完整对话（不再被顶部团队面板压缩）；右侧栏
> 上=团队工作流驾驶舱、下=资源面板；右上角菜单收缩。同步决策：
> - 计划/团队面板 → 被团队驾驶舱（TeamFlowPanel 时间轴/工作流）
>   完全覆盖，移除
> - 权限面板 → 顶栏 PermissionModeSelect 已覆盖，移除
> - 知识库/MCP/技能 → 合并为右侧资源面板（ResourceTabsPanel，
>   Tab 切换 + 已加载数量徽章），经典布局下仍可从右上角菜单 dock

实现（ChatViewport 双布局，偏好持久化 `chat_layout_mode`）：
- focused（默认）：完整对话 + 右侧栏（TeamFlowPanel 上、
  ResourceTabsPanel 下，资源内容 JSX 与经典 dock 共用一次定义）
- classic：旧布局原样保留（TeamFlowPanel 在对话顶部 + PanelDock
  菜单开关），右上角一键切换往返
- PanelKey 类型收缩为 'mcp' | 'skill' | 'knowledge'，localStorage
  旧 layout 自动 drop 被移除的 key（loadPanelLayout 既有过滤）
- TaskPanel/PermissionPanel/TeamPanel 组件文件保留但不再挂载

测试 4 项：资源面板 Tab 齐全、PanelKey 收缩、布局切换三要素
（持久化键/两模式/文案键）+ 旧面板不挂载、i18n 双语。真实浏览器
E2E：专注默认（驾驶舱+资源Tab）、对话区全宽、切经典（团队面板回
顶部）、经典菜单仅 3 项、刷新保持偏好、切回专注全部断言通过。

### 28. 团队培养资产保留 + 成员团队会话跳转（2026-09-07）

> 用户反馈：① 点成员"进入会话迭代"进入空白独立会话，看不到成员
> 在团队任务中的聊天内容；② 部署窗口期动态 import 404 卡死错误页
> （RouteError 的"重试"对被缓存的 lazy import 失败无效）。
> 根因：官方 TeamDelete 全灭式级联（成员 agent+session+team 记录
> 全删），解散后映射无从追溯；React Router 捕获路由懒加载错误，
> main.tsx 的 unhandledrejection 自愈监听接不住。

修复（app/team_preserve.py + 前端）：
- **软解散 patch**：启动时 monkey-patch RedisStorage.delete_team——
  只清 leader session 的 team_id（view.team 判定失效=解散），
  成员 agent/成员团队 session/team 记录全部保留
- **GET /team-sessions/{leaderSid}**：返回历次团队（含解散）的
  成员 agent↔session 映射；成员读取兼容 pydantic/dict 两种形状
- **前端跳转**：无活跃团队时 flowMembers 从 team-sessions 补
  sessionId，"进入会话迭代"跳成员的**团队会话**（有任务上下文，
  可继续对话介入培养——成员 session 是标准 agent session）
- **RouteError 自愈**：识别动态 import 失败自动整页刷新
  （sessionStorage 防死循环）
- **布局按钮文字化**："经典布局/专注布局"文字+图标（纯图标用户
  找不到）

测试 +9：test_team_preserve.py（软解散保留/幂等/不存在/映射 API/
解散标记/空会话，FakeStorage duck-typing 不依赖真实 Redis）+
webui_static 3 项（RouteError 自愈/teamSessions 调用/按钮文字）。
真实浏览器 E2E：布局按钮可见、成员团队会话路由有内容、点"进入
会话迭代"跳转成功且输入框可用（可介入对话）、API 映射正确。

注：高考团队（09-03）等历史团队已被旧版硬删，无法追溯——软解散
只保护之后的团队。

### 29. TeamDelete 双层防护 + 团队 hint 紧凑化（2026-09-07）

> 用户产品逻辑："大A 只是 Team 的入口，培养好的大A+Team 完整绑定
> 开放给别人用——完全不能接受随便把 team 删了，删除一定要我确认。"
> 同时反馈：消息流里的团队消息折叠块（"下拉"）与右栏驾驶舱内容重复。

**关键发现（前版软解散 patch 的 bug）**：
`SessionService.delete_team` 在调 `storage.delete_team` 之前就先对
每个成员执行 `delete_agent`（created 成员连 agent 物理删除）/
`delete_session`（invited 成员团队会话删除）——patch 在 storage 层
拦得太晚，成员早已被删。

双层防护（app/team_preserve.py 重写）：
- **第一层 强制确认**：patch `TeamDelete.check_permissions` →
  bypass-immune ASK。DEFAULT/ACCEPT_EDITS 必弹用户确认卡，
  "始终允许"规则压不住（bypass-immune 契约）；DONT_ASK 转 DENY；
  EXPLORE 本就 DENY；BYPASS 按框架契约放行（用户显式完全信任）
- **第二层 软解散**：patch `SessionService.delete_team`（工具的
  唯一执行路径）→ cancel_session_run 取消成员运行（防僵尸）+
  清 leader session team_id（view.team 判定解散），成员 agent/
  成员团队 session/team 记录全部保留。storage 层级联（用户删
  leader 会话的连带清理）不受影响

**团队 hint 紧凑化**（前端）：
- `TeamHintCompactContext`（ASMessageBubble 导出）：主理会话
  `compactTeamHints={isLeader}`，团队消息（`<team-message from=…>`）
  渲染为紧凑单行「📤 成员名 已向主理人汇报（详见右侧团队面板）」
- 成员会话/普通会话无驾驶舱，hint 保持完整折叠块
- useContext 必须在组件顶层调用（Hooks 规则，不得在 switch case 内）

测试：test_team_preserve.py 重写为 8 项（权限层 2 + 服务层 3 +
API 层 3，FakeSessionService duck-typing）；webui_static +3
（TestSrcTeamDeleteGuard）。全量 449 项通过。真实浏览器 E2E：
主理会话紧凑单行 + 驾驶舱完整、成员会话 hint 保持完整全过。

### 30. 团队驾驶舱重复渲染修复 + 唯一性断言规则（2026-09-07）

> 用户反馈：专注布局下对话区顶部仍有团队面板（应只在右栏）。

**事故根因（双漏）**：
1. 批量脚本替换 ChatViewport.tsx 时，顶部 TeamFlowPanel 一段的
   缩进与模式串不匹配且 `str.replace` 未加 assert → 静默失败，
   classic 门控没加上；专注布局下顶部+右栏渲染了两份驾驶舱
2. 当时 E2E 只断言"存在性"（页面含驾驶舱文本）未断言"唯一性
   +位置"，重复渲染漏网（与 §16"空状态误判成功"同类教训）

**规则**：
- 对可重复组件的 E2E 必须断言**数量恰为预期** + **位置正确**
  （本例：驾驶舱 Tab「团队动态」按钮数 === 1，且专注布局 x >
  60% 视口宽=右栏、经典布局 x < 60%=顶部）
- 批量文本替换必须 assert 命中，未命中立即报错（不接受静默）
- 静态锁：顶部渲染必须含 `layoutMode === 'classic' && isLeader
  && sessionId` 门控；`<TeamFlowPanel` 挂载点恰好 2 处
  （classic 顶部 + focused 右栏）——TestSrcNoDuplicateTeamPanel

修复后 E2E 四断言全过：专注唯一右栏、顶部干净、经典回顶部、
切换+刷新保持。全量 451 项通过。

### 31. 对话列与顶栏同宽对齐（2026-09-07）

> 用户反馈："顶栏宽、对话窄，对不上；对话与右侧小组面板之间留
> 空白。"

根因：消息/输入列固定 48rem（768px）居中，顶栏全宽——中间面板
宽于 48rem 时上下错位，右侧出现大片空白。修复：`--chat-content-w`
改为 100%（填满中间面板），顶栏/消息/输入框同宽对齐，与右栏驾驶
舱仅余边距。

注意测量方法（E2E 教训）：对齐断言要量**输入卡片**（textarea 的
`rounded-[32px]` 祖先）而非 textarea 本身（后者含内边距，天然窄于
容器，会误判 FAIL）。

静态锁：TestSrcChatWidthAlignment——宽度变量必须 100%、不得回退
48rem。实测：顶栏(x=329,w=721) === 输入卡(x=329,w=721)，距右栏
驾驶舱 19px。

### 32. Token 用量计量与统计（2026-09-07，消费计量 v1）

> 用户需求："我要知道我这个账号的消耗情况——模型、大A/小A、
> 输入、输出。"

**后端**（`app/usage_metering.py` + `tests/test_usage_metering.py`，21 用例）：
- 实时钩子：patch `upsert_message`，官方 `Msg.usage` 落库时按
  `(日期, agent_id, 模型)` 聚合到 `agentforge:usage:{user}` Hash；
  `agentforge:usage:seen` Set 对消息 id 去重——流式回复同 id 多次
  upsert（usage 从空到终值）只计一次
- 存量回填：启动钩子后台跑 `backfill_usage`（幂等，同靠 seen Set）。
  **踩坑**：用户发现必须扫会话**记录键**
  `agentscope:user:{u}:session:{sid}`（每次 upsert 必写），不能扫
  `agent:{aid}:sessions` 索引键（仅新建时写）——只认索引会漏用户，
  test_backfill_multiple_users_and_sessions 回归锁定
- 查询 API `GET /usage/summary?days=N`：复用认证注入的 X-User-ID
  多租户隔离；总计/按日期/按 agent/按模型四维度，日期倒序
- 容错铁律：计量内部异常必须吞掉（消息落库优先），钩子 try 块
  兜底——test_metering_failure_does_not_block_upsert

**前端**（`pages/usage/`）：侧边栏 ChartPie 入口 → /usage 页面：
总计四卡片（输入/输出/缓存/调用次数）+ 每日趋势（纯 CSS 双色条，
无图表库依赖）+ 大A/小A 表 + 模型表（含占比条）；7/30/90 天窗口
切换；空态/错误态/骨架齐备。静态锁 TestSrcUsagePage（7 用例）：
路由/导航/四维度组件/状态/API 客户端/i18n/后端挂载。

验证：pytest 480 项通过；浏览器 E2E 登录→导航→页面渲染→窗口
切换全过（当时后端未重启故空态为预期）；`curl /usage/summary`
联通。

### 33. `test_local_skill_hub.py`（17 例）+ `test_mcp_tools.py`（6 例）—— 本地技能中心与 MCP 工具清单

> 用户需求："技能/ MCP 装了之后，我要能看到每个包里到底有什么工具、
> 什么说明，再决定怎么用。"

**本地技能中心**（`app/local_skill_hub.py`，17 用例）：目录扫描 /
> 搜索 / 分页 / 详情 / 下载全链路，读 registry 目录结构不访问网络；
> manifest + SKILL.md 双文件解析（缺一降级不崩溃）。

**MCP 工具清单**（`app/mcp_tools.py`，6 用例）：`GET /mcp-tools/{id}`
> 列出指定 MCP server 的工具清单；monkeypatch `_list_tools_via_probe`
> 不访问真实 server，storage 用假 record。

### 34. `test_agent_share.py`（36 例）—— 智能体跨账号共享 + 版本化发布

> 用户需求（v1，2026-09-07）："我发布之后，能选择哪些账号可以看到
> 我的大A/小A。"
> 用户需求（v2，2026-09-08）："发布的时候要有版本控制——同一智能体
> 的 v2（文科版）、v3（理科版）要能分别发布、对外重命名。"

**共享 v1 + 版本可见性**（策略、管理端点与版本权限，26 例）：
- 官方 `ResourceAccessPolicyBase` 的 Redis 实现——授予的 agent 以
  只读 ResourceRef 并入官方 ResourceAccessService（列表合并
  editable=false / resolve_for_edit 403 / 会话聊天放行，全链路官方自带）
- 三种可见性：私有（默认，他人 404）/ 指定账号 / 全部账号
- 管理端点 `GET /agent-share/mine`、`PUT /agent-share/{id}`（发布/
  改可见性）、`DELETE /agent-share/{id}`（取消发布）
- 测试用**真类** `ResourceAccessService` 验证策略与官方链路集成

**版本化发布 v2**（`TestVersionedPublish`，8 例）：
- `POST /agent-share/publish`：版本快照 → 复制出独立对外产品
  （可重命名）+ 共享；发布物元数据（源 agent + 源版本）入 Redis
- `GET /agent-share/pubs`：我的发布物列表（含溯源）
- 取消发布清理 pubmeta（防已下架产品残留列表）
- 发布必须是 users/public 模式（发布必共享）；版本快照必须存在

**本体删除级联**（`TestAgentDeleteShareCleanup`，2 例）：
- 任意入口成功删除 `DELETE /agent/{id}` 后，清理共享主记录、授权用户、
  public/owner 索引、发布元数据和发布物索引（含历史脏索引）
- 官方删除返回失败时不得误清共享数据

### 35. 账户中心（2026-09-07/08，test_usage_metering 扩展 28 例 + test_webui_static 扩展）

> 用户需求："一个账户界面，直观看到我的消费情况（模型、大A/小A、
> 输入、输出）+ 管理我发布的智能体。"

**消费概览 v2**（`/account` Tab）：产品维度聚合——"大A及team"
> （含全部成员成本）与"独立小A"分行，按模型/输入/输出/调用次数
> 拆分；团队行可展开成员成本构成；归因按当前团队结构（member→
> leader 映射），消费记调用方账号（SaaS 惯例）。test_usage_metering
> 21→28 例（+7 产品维度聚合）。

**入口收敛**：旧 /usage、/share 独立页并入 /account 双 Tab
> （消费概览 + 智能体管理），路由保留重定向；侧边栏 ChartPie/Share2
> 双图标收编为 CircleUserRound 单入口。

### 36. `test_agent_version.py` 扩展（33 例）+ 智能体管理界面（2026-09-08 v2）

> 用户需求："发布新版本怎么没反应？""我要基于基础版优化出文科版、
> 理科版两个智能体""管理界面不要只有发布——增删改查都应该有。"

**save-version 修复**：配置未变时曾静默复用旧版本号（界面无反馈
> = 用户遇到的"点了没反应"）→ `force=True` 每次发版必新增。

**复制智能体**（`TestDuplicate`，+8 例）：`POST /agent/{id}/duplicate`
> 从当前配置或任意版本快照分叉新个体；所有权按 storage.get_agent
> 键控校验（防列表含共享智能体导致的越权复制）；团队成员智能体
> 随团队生命周期管理，不支持复制（400）。

**前端智能体管理页**（`AgentManagement.tsx` 取代 ShareManagement）：
> 完整增删改查（新建/编辑/删除）+ 版本管理 + 复制 + 发布版本 +
> 可见性 + 我的发布物（溯源展示）+ 共享给我（只读）。静态锁
> `test_share_management_migrated` 重写：断言 CRUD 组件、三条版本化
> 链路（duplicate/publish/publications）及旧页面已删。

验证：pytest 532 项通过；浏览器 E2E 全链路（发版 v1/v2 → 复制
> "高考志愿兵-文科" → 发布"高考志愿兵-文科版"给指定账号 →
> 发布物溯源展示）实测通过。

### 37. `test_session_flow.py`（22 例）+ `TestSrcSessionFlow` 静态锁（2026-09-08 v3）

> 用户需求："暂停/继续、任意位置重新对话、流程重新启动"。
> 后端 `app/session_flow.py` 五端点：`/sessions/{sid}/truncate`
> （归档+LTRIM 截断+context 同步）、`/sessions/{sid}/restart`
> （上下文归零保历史）、`/team-flow/{sid}/pause|resume`（团队级
> 停止/唤醒）、`/sessions/{sid}/flow-archive`（被删消息副本查询）。

| 组 | 覆盖点 |
|---|---|
| TestTruncate | 5 条截到 3 条（消息 List LTRIM + state.context 同步 + summary/reply_context 重置 + 归档副本）；截最后一条（无删除不写归档）；消息不存在 404；运行中 409 零改动；会话不存在 404；未认证 401；**context 时间锚点退化**（截断点不在 context 时按 created_at 对齐，宁少删不误删）；**permission_context/tasks_context 保留**（已授权工具不必重新授权）；归档查询 API |
| TestRestart | context/summary/reply 归零 + 消息历史完整保留；运行中 409；**团队 leader 重启先 cancel 全部成员** + leader HITL 中断 + 团队绑定保留（重启≠解散）；非团队不调 cancel/interrupt；已解散团队跳过成员；会话不存在 404 |
| TestTeamFlow | 暂停 = leader interrupt + 全部成员 cancel；非团队会话暂停幂等；404；继续 = enqueue wake（官方 input:None 语义，唤醒 leader 自行恢复调度）；404 |
| TestStateSemantics | 截断后新消息不复活旧消息；同截断点重复调用幂等（第二次 removed=0 不重复归档） |
| TestSrcSessionFlow | 前端三层接线静态锁：API 层（5 方法+2 类型+再导出）、hook（truncateAt/restartFlow+reloadToken 挂进 effect 依赖）、气泡（Split 按钮+运行中隐藏）、ChatContent（空闲才传 onTruncateAt+继续按钮限定有历史）、ChatViewport（重启按钮空闲禁用+两种布局团队接线各 2 处）、TeamFlowPanel（暂停/继续+busy）、i18n 全词条（zh/en） |

**测试基建要点**：fakeredis 必须 `decode_responses=True` 对齐官方
RedisStorage（否则 `smembers` 返回 bytes，`list_teams` 拼 key 失配
—— 真实环境冒烟发现的坑，已写进 fixture 注释）。

验证：pytest 561 项通过；真实环境 API 冒烟（登录 → 新建会话 →
Redis seed 5 条消息 → truncate 保留 3 归档 2 → flow-archive 副本
核对 → restart 后 context 空历史保留 → pause/resume 幂等 → 清理）；
浏览器 E2E：重启确认对话框+toast、继续按钮触发 wake、Split hover
按钮+确认对话框、团队驾驶舱暂停/继续按钮（真实团队数据渲染）。


### 38. `TestBrandAgentForge` 静态锁（2026-09-08，7 例）

> 用户需求："平台是 Agent Forge，基于 AgentScope 二次开发没问题，
> 但 UI 太像原生 AgentScope 了，帮我切换成 Agent Forge。"
> 品牌触点全量切换：favicon/title/侧边栏 logo/登录页/错误文案；
> `@agentscope-ai/...` npm 包导入不属于品牌，不检查。

| 触点 | 变更 | 锁 |
|---|---|---|
| SPA 入口 | title→Agent Forge，favicon→/agentforge.svg | `test_index_html_brand` + `test_deployed_favicon` |
| 侧边栏 | 新 mono 标识（铁砧+火花）+ hover 提示 | `test_sidebar_logo` |
| 品牌资产 | 新增 agentforge_mono/agentforge.svg；旧 agentscope 三件套删除 | `test_brand_assets_exist_and_old_removed` |
| 登录页 | 「Agent Forge · 智能体控制台」+🔨+锻造副标题 | `test_login_page_brand` |
| 后端放行 | STATIC_EXACT 增 /agentforge.svg | `test_auth_static_allowlist` |
| 翻译文案 | errorNotAgentScope→errorNotService（键+值去 AgentScope） | `test_i18n_no_visible_agentscope_brand` |

验证：pytest 568 项通过；浏览器 E2E 6 项全 PASS（登录页品牌/标签页
标题/侧边栏 logo/favicon 可达/console 无错/chat 正常渲染）。

### 39. `test_patent_agents.py`（9 例）——专利撰写小A基础工具（2026-11-02）

> 三个独立小A按“权利要求 → 背景技术与发明内容 → 具体实施方式”顺序
> 使用。带 `agentforge-profile: patent-drafting` 标记的智能体在每个新会话
> 自动获得专利检索、详情取证、期刊检索及三个确定性文本检查工具。

| 组 | 覆盖点 |
|---|---|
| 模板契约 | 三份模板存在；均要求检索、详情取证、风险边界；具体实施方式支持完整初稿和逐项改进 |
| 条件装配 | 专利 profile 获得 6 项工具；普通智能体不受影响；全部工具只读 |
| 后端复用 | 注册包参数原样交给 `smart_writing.call_backend`，响应保持结构化 JSON |
| 权要结构 | 断号、后项/自身/不存在引用会报错；合法独立项、从属项和跨项引用通过 |
| 术语一致性 | 规范术语跨章节缺失及禁用别名命中均给出位置 |
| 支持覆盖 | 权要特征在指定说明书章节生成覆盖矩阵，支持调用方给出允许替代表述 |

验证：专用测试 9 项通过。真实 `:30111/mcp` 调用确认 MCP 路由与后端可达；
布尔检索返回规范 `data/cnt_dict`，自然语言探测当前可能返回
`no skill returned any result`，智能体必须将空召回视为检索边界，不能据此
宣称无相似专利。

### 40. `test_agent_share_mcp.py`（16 例）——发布物最薄 MCP 封装（2026-11-02）

> 每个版本化发布物复用同一 Streamable HTTP 端点模板，对外只暴露
> `run_task` 和 `get_run`。前者异步启动任务，后者返回运行状态、团队成员
> 职责、可验证行动轨迹和最终答案；原始 thinking、凭证和内部路径不外泄。

| 组 | 覆盖点 |
|---|---|
| 协议与授权 | MCP 工具清单；owner/public/指定账号可见性；无权访问统一拒绝 |
| 运行隔离 | 同一会话连续运行按消息锚点切窗；run_id 按账号与发布物隔离 |
| 团队透明度 | 聚合主理人和小A状态、职责及可验证消息；过滤 thinking 并脱敏敏感字段 |
| 长历史 | 分页回溯到运行起点；超过 5000 条时明确返回截断标记 |
| 凭证 | Cookie 管理端签发发布物 scoped Bearer；Redis 只存摘要；跨发布物不可复用；支持统一吊销 |
| 生命周期 | 取消发布或删除发布物时吊销凭证；durable pending 保证删除中断后可恢复清理 |

验证：`test_agent_share_mcp.py`、`test_agent_share.py`、`test_auth_api.py`
合计 97 项通过。真实 `:30000` 冒烟中，Basic 和签发后的 Bearer 均成功
列出 `run_task/get_run`；前端相关文件 ESLint 通过，TypeScript + Vite
生产构建通过。

### 41. 对话临时空间与账号项目（2026-11-02，21 例）

> 普通对话可以直接创建临时文档，删除对话时清理；明确创建或选择项目
> 后，多个会话共享持久项目空间。空间内的日常文件操作默认授权，不再为
> `mkdir`、读取和写入文档逐次询问。

| 组 | 覆盖点 |
|---|---|
| 账号隔离 | 磁盘路径与内存缓存均按账号隔离；相同显式 workspace id 不串空间 |
| 生命周期 | 临时空间删除；仍被会话引用时保留；项目空间永久保留；团队成员引用纳入检查 |
| 存量迁移 | 仅迁移账号自有智能体的旧目录到最早会话；共享智能体目录不向访问者迁移 |
| 路径边界 | 拒绝绝对路径、`..` 和符号链接逃逸；响应不返回宿主路径或 Git 状态 |
| 默认权限 | 新会话采用 `accept_edits`，当前空间内读写与文件系统命令自动允许 |
| 项目注册 | 项目按账号隔离、名称规范化与去重、原子持久化、共享 workspace 绑定 |
| 启动回归 | 生产模块可在 lifespan 服务创建前完成导入，防止中间件提前取 state 导致启动失败 |

验证：定向测试 21 项通过；TypeScript + Vite 生产构建通过；真实 `:30001`
登录后 `GET /workspace-projects` 返回 200，聊天深链接加载新构建产物且 HTML
语言为 `zh-CN`。全量 783 项在既有同步 TestClient 用例
`test_agent_share.py::TestShareApi::test_publish_users_mode` 挂起，单独运行同样挂起，
未得到全量完成结论。

### 42. 论文细节优化七小A（2026-11-03，12 例）

> 按“研究现状 → 科学问题 → 方法论 → 实验与结果 → 讨论 → 结论”组织
> 主链，关键词在摘要完成后独立处理。带
> `agentforge-profile: paper-drafting` 标记的智能体在新会话自动获得
> 有界的 `search_journals`，并进入长期使用观察档案。

| 组 | 覆盖点 |
|---|---|
| 模板契约 | 七份模板名称完整；均含论文 profile、检索能力、事实边界和思维链保护 |
| 条件装配 | 论文 profile 仅获得只读期刊检索；普通 Agent 不受影响 |
| 能力展示 | 管理页显示学术文献检索，不误显示专利质量工具 |
| 使用观察 | 论文小A消息按账号和 Agent 长期归档，删除会话后仍可用于优化分析 |
| 幂等安装 | 首次按名称新增；同名提示词更新时保留 Agent ID；重复运行不产生副本 |
| 账号边界 | 不存在的账号明确跳过，不向近似用户名误安装 |

验证：新增及直接相关回归 24 项通过；真实 Redis 回读确认 `admin`、
`zhaohongyu` 各有 7 个论文小A，二次预演均为“新增 0、未变化 7”；
`:30000` 服务重启后状态为 `active`。

### 43. 论文小A的 AnySearch 与检索熔断（2026-11-03，13 例）

> 七个论文小A在期刊检索之外自动获得只读 `anysearch_search`，用于补充
> 最新网页、官方项目页和术语交叉核验。网页结果只返回标题、URL 和摘要，
> 不得替代论文元数据、论文原文或本研究的实验数据。

| 组 | 覆盖点 |
|---|---|
| AnySearch 协议 | 官方端点、可选 Bearer、请求字段和响应结构校验 |
| 请求边界 | 单会话最多两次、同参缓存、首次失败熔断、串行调用 |
| 期刊检索 | 单会话最多两次；后端首次异常后停止重试并引导使用 AnySearch |
| 模板契约 | 七份模板均说明两种检索的分工、调用上限和网页证据边界 |
| 能力展示 | 能力页同时展示期刊与联网检索，并标记联网搜索已启用 |

验证：新增及直接相关测试 13 项通过；相关回归排除既有同步
`TestMcpEndpoint` 挂起组后 49 项通过。真实 AnySearch 免 Key 请求限制为
2 条结果，返回 `code=0`、来源 URL 和摘要；未调用期刊或专利远端服务。
更新后的七份提示词已同步到 `admin`、`zhaohongyu`、`wangnan`，幂等
回验均为“新增 0、更新 0、未变化 7”。

### 44. 论文研究现状智能体单体优化（2026-11-04，15 例）

> 本轮只优化“论文研究现状智能体”，不改变其他六个论文小 A，也不引入
> 大 A 编排。提示词根据 `upgrade_pro/paper_detail_upgrade/api_readme.md`
> 的 Related Works、论文检索和参考文献要求更新。

| 组 | 覆盖点 |
|---|---|
| 检索契约 | 明确 `title:/abst:/keywords:` 中文布尔检索格式 |
| 请求负载 | 论文小 A 期刊检索默认并限制为最多 20 条 |
| 证据边界 | AnySearch 网页线索与正式期刊文献分离，缺失元数据不补写 |
| 综述结构 | 技术流派分类、演进逻辑、适用边界、共同局限和研究缺口 |
| 失败行为 | 非法检索式本地拒绝；失败后停止重试；不为凑数生成文献 |

验证：直接相关测试 15 项通过；三个账号的研究现状智能体提示词已同步。
真实流式验收两次均无 `RUN_ERROR`；第二次验收确认期刊无结果时正式文献证据表为空，
网页结果单列为线索。

### 45. 论文其余六个小 A 的单体提示词优化（2026-11-04，16 例）

> 本轮继续保持七个 Agent 独立运行，不引入大 A 编排和跨 Agent 自动传递。
> 科学问题、方法论、实验与结果、讨论、结论、关键词六个 Agent 根据
> `api_readme.md` 中对应的 C14、C16-C20 结构补充输入边界、输出内容和证据规则。

| 组 | 覆盖点 |
|---|---|
| 科学问题 | 痛点类别/严重度、根因假设、Gap 证据和挑战对齐 |
| 方法论 | 研究方案、3-5 个关键方法、痛点对应关系和复现边界 |
| 实验与结果 | 实验设计模式与真实结果模式分离，预期结果不得冒充观察结果 |
| 讨论 | 观察结果、统计证据、机制假设和局限性分级 |
| 结论 | 300-500 字结论、贡献点、未来工作和证据回溯 |
| 关键词 | 3+2+1 分层、术语核验和标题去重 |
| 可审计摘要 | 每个 Agent 输出依据、判断理由和待确认项，不输出隐藏思维链 |

验证：直接相关测试 16 项通过；提示词更新后将分别在三个账号中回验，
每个账号只更新对应维护中的论文小 A。

### 46. 论文小 A 线上真实验收（2026-11-04）

使用线上 `http://116.204.102.229:30000`、`admin` 账号和豆包凭据，
通过 `scripts/smoke_paper_agents_live.py` 串行创建临时会话并触发真实模型。

| Agent | 结果 |
|---|---|
| 论文研究现状智能体 | PASS；真实输出约 3.8k-5.2k 字符，正式文献与网页线索分离 |
| 论文科学问题智能体 | PASS；真实输出约 4.3k 字符 |
| 论文方法论智能体 | TIMEOUT；等待 180 秒仍处于运行状态，不能判定通过 |
| 论文实验与结果智能体 | PASS；真实输出约 6.8k 字符，缺少数据时进入设计模式 |
| 论文讨论智能体 | PASS；真实输出约 5.9k 字符 |
| 论文结论智能体 | PASS；真实输出约 2.6k 字符，缺少结果时保持证据边界 |
| 论文关键词智能体 | PASS；真实输出约 3.0k 字符 |

发现：线上删除临时会话时，记录实际已删除，但接口返回 HTTP 500。原因是
会话删除后的临时工作区引用检查异常未被隔离。已在 `app/session_defaults.py`
改为 best-effort 清理，并增加回归用例；该修复需要随服务重新部署后才能在线生效。

### 47. 专利交底书新增两个小 A 线上真实验收（2026-11-05）

使用线上 `http://116.204.102.229:30000`、`admin` 账号和豆包凭据，
通过 `scripts/smoke_patent_agents_live.py` 串行创建临时会话并传入实际结构化数据。

| Agent | 结果 |
|---|---|
| 专利技术信息提取智能体 | PASS；返回约 350 字符结构化结果，未调用检索工具 |
| 专利技术方案分析智能体 | PASS；返回约 2772 字符结构化分析结果 |

两个 Agent 的临时会话均实际删除；线上删除接口仍返回已知 HTTP 500，
原因与第 46 节相同，待会话清理修复部署后消除。

### 48. 大 A / 小 A 选择器线上发布验收（2026-11-06）

本地前端选择器测试 `92 passed, 2 skipped`，生产构建通过；先启动独立
`30001` 预览供用户登录验收，确认类型切换、分组、最近互动排序和长名称显示后，
按增量方式将 `webui-src/dist` 更新到线上 `30000` 的 `webui/` 目录。

线上验证：通过 `30000/auth/login` 登录成功，登录后首页返回本次构建的
`index-BNv1_ixo.js`；`30001` 临时预览随后关闭，未停止或重启线上服务。

### 49. 专利提取小 A 大批量切片与导出（2026-11-06）

提取小 A 的前端运行时增加批处理保护：结构化 `content` 列表默认每片最多
500 条，并增加单片字符预算；上一片会话完成后才发送下一片，避免多个请求并发
撞上同一会话的运行锁。每片仍通过现有 SSE 消息流显示，全部结果可从消息区下载为
JSON 或 Markdown。

验证结果：专利、安装脚本和 Web UI 回归 `109 passed, 2 skipped`；TypeScript
生产构建通过；线上 `30000` 登录成功并返回最新 `index-CR4s-ssy.js`。
无浏览器自动化依赖，因此未伪造“浏览器点击通过”；线上资产和登录链路已做 HTTP
验证，批处理切片逻辑由构建和静态回归覆盖。

### 50. 专利小 A 后端工具装配与真实回归（2026-11-06）

重启 `agentforge-svc` 加载已测试的工具工厂和能力展示修复。线上能力接口实测：

- 提取 Agent：仅平台基础工具 + `SolidifySkill`，`web_search_enabled=false`。
- 分析 Agent：平台基础工具、`SolidifySkill`、`search_patents`、
  `get_patent_details`、`search_journals`、三个专利质量检查工具和
  `anysearch_search`，`web_search_enabled=true`。

低并发串行真实 smoke 通过：提取 Agent 返回 421 字符，分析 Agent 返回 2891
字符；两个临时会话均以 HTTP 204 清理。首次运行的沙箱网络失败不计入结果，使用
受限网络权限复测通过。

### 51. 专利批处理 JSON 导出结构（2026-11-06）

JSON 下载由单一 `result` 文本封装调整为 `results` 数组：每个批次会尝试
解析为 JSON，无法解析的模型输出仍以原文字符串保留，避免批次结果丢失；Markdown
下载和消息区展示保持不变。

验证：前端 TypeScript 生产构建、Web UI 静态回归和专利小 A 相关后端回归通过。

### 52. 专利小 A 上下文策略（2026-11-06）

提取小 A 将每次新的用户输入视为独立提取任务：开始前清空旧的会话上下文、
摘要和中间状态，但保留权限与任务状态，不改变工具装配；当前请求使用接近模型
上限的上下文窗口，避免处理中途压缩当前输入。分析小 A 保留历史，使用更早的
压缩阈值和较低的工具结果预算，控制检索证据累积。普通智能体继续使用平台默认策略。

验证：上下文策略、提取/分析工具契约和安装回归 `20 passed`；未修改提取小 A
的无检索工具边界，也未修改分析小 A 的专利、期刊和 AnySearch 工具链。

## 维护规则

1. **改哪个模块，跑哪个模块的测试 + 全量**：改 `app/auth.py` → `pytest tests/test_auth_unit.py tests/test_auth_api.py` 后再 `pytest` 全量
2. **行为变更同步用例**：接口语义变化（状态码、错误文案、字段）必须同步修改对应断言，并在 commit message 里注明
3. **新增功能先补用例**：新端点/新工具类遵循现有模式（TestClient + monkeypatch + tmp_path）
4. **发现 bug 先写失败用例再修**：修复必须让新用例转绿且不破坏存量（如本次 `compare_digest` 非 ASCII bug）
5. 真模型/真中台链路验证走 `smoke` 标记与 `scripts/smoke_*.py`，不混入单元回归
6. **前端改动必须以"真实数据出现"为验证标准**（事故教训）：浏览器验证时"暂无会话"、"加载中"、空列表、欢迎语等**空状态一律判 FAIL**，必须看到历史会话消息渲染、表单字段加载、凭据列表非空等真实数据才算 PASS；无法进 pytest 的前端运行时行为（如 `new URL` 基址）至少要补进 `test_webui_static.py` 的补丁/产物断言

## 已知边界（不在单测覆盖内）

- 形态 B `agent_service_app.py` 的 `create_app` 依赖真实 Redis/官方 webui 静态产物，其鉴权与心跳逻辑已分别由 test_auth_api / test_startup_hook 覆盖，端到端验证走 smoke 脚本
- systemd 单元、Docker Redis、安全组等部署项属运维验收范围
