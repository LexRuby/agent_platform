# 测试用例说明（TESTS.md）

自动化测试体系：**384 个用例**，`pytest` 一条命令全量回归（约 18 秒，不依赖任何真实外部服务）。

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
