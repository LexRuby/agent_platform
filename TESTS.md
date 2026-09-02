# 测试用例说明（TESTS.md）

自动化测试体系：**203 个用例**，`pytest` 一条命令全量回归（约 2 秒，不依赖任何真实外部服务）。

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

### 5. `test_console_api.py` — 控制台 API /api（29 例）

| 组 | 覆盖点 |
|---|---|
| TestToolsEndpoint | 内置工具列出；MCP 工具注册后合并（ref 格式 `mcp:srv:tool`） |
| TestMcpRegistration | **注册失败回滚不留脏数据**（502）；刷新不存在 404；刷新更新工具；删除；**list 永不暴露 token** |
| TestAgentEndpoints | 建查；缺失 404；draft 可改；**locked 改 409**；锁定要求 draft；解锁恢复 draft；**发布要求 locked（draft 直接发布 409）**；锁定缺失 404 |
| TestWorkspace | 对话历史持久化（user/assistant 交替）；**锁定后打磨对话 409**；不存在 404；清空；fake 回复包含工具数 |
| TestPublishedHall | 只列 published；发布后可对话；**draft/locked 不能走对外入口（404）**；缺失 404；**对外会话不污染打磨工作空间** |

### 6. `test_tools.py` — 中台工具层（13 例）

| 组 | 覆盖点 |
|---|---|
| TestPost | 成功；payload 原样 JSON 传递；**500 后重试成功**；重试耗尽（次数 = RETRIES+1）+ RuntimeError；连接错误同样重试；4xx 也走重试路径 |
| TestToChunk | JSON 序列化；中文不转义；**超长截断到 8000** |
| TestWritingTools | 端点绑定 `/writing/generate`；input_schema 必填字段；元数据；call 全链路（含 mock 中台回包） |

### 7. `test_mcp_client.py` — MCP 客户端协议（13 例）

| 组 | 覆盖点 |
|---|---|
| TestParseResponse | 纯 JSON；SSE 流（跳过无 id 通知）；SSE 无 id 报 McpError |
| TestRpc | JSON-RPC 信封（jsonrpc/method/id）+ Accept 头；HTTP 500 抛错；JSON-RPC error 抛错；Bearer token 注入 |
| TestConnect | initialize 握手 + initialized 通知 + session id 传递；无 session 则不发通知 |
| TestListTools | 全流程 connect→tools/list；字段映射；缺 schema/description 容错 |
| TestCallTool | 多文本块拼接、非文本块忽略；工具错误抛 McpError；空结果返回空串 |

### 8. `test_main_api.py` — 工单服务 HTTP 层（19 例）

隔离：环境变量指向 tmp + `importlib.reload(app.main)` + 假 runner。

| 组 | 覆盖点 |
|---|---|
| TestTemplates | 模板列表 |
| TestCreateTask | 创建停在人工环节（waiting_human/assignee/历史）；未知模板 400；缺必填变量 400；持久化并可列表 |
| TestGetTask | 缺失 404；按状态过滤 |
| TestSubmit | 提交恢复至完成（历史顺序 + submit 审计）；错误环节 409；缺失任务 409 |
| TestSkip | 跳过当前人工环节；跳过计划环节（最终历史不含） |
| TestRedo | 重做已完成环节插入副本（不中断当前人工；提交后副本先于后续执行） |
| TestReassign | 改派成功；错误环节 409 |
| TestErrorMapping | 业务异常→400/404/409 完整映射 |

### 9. `test_startup_hook.py` — ASGI lifespan 钩子（5 例）

startup complete 触发回调；**重复 complete 仅触发一次**；startup failed 不触发；shutdown 不触发；普通 HTTP scope 透传无注入。

### 10. `test_workflow.py` — 工单引擎（原有，8 例）

创建停在人工门；提交完成；跳过当前/计划；失败重做；完成环节重做插副本；改派；缺变量；错误提交拒绝；持久化。

## 维护规则

1. **改哪个模块，跑哪个模块的测试 + 全量**：改 `app/auth.py` → `pytest tests/test_auth_unit.py tests/test_auth_api.py` 后再 `pytest` 全量
2. **行为变更同步用例**：接口语义变化（状态码、错误文案、字段）必须同步修改对应断言，并在 commit message 里注明
3. **新增功能先补用例**：新端点/新工具类遵循现有模式（TestClient + monkeypatch + tmp_path）
4. **发现 bug 先写失败用例再修**：修复必须让新用例转绿且不破坏存量（如本次 `compare_digest` 非 ASCII bug）
5. 真模型/真中台链路验证走 `smoke` 标记与 `scripts/smoke_*.py`，不混入单元回归

## 已知边界（不在单测覆盖内）

- 形态 B `agent_service_app.py` 的 `create_app` 依赖真实 Redis/官方 webui 静态产物，其鉴权与心跳逻辑已分别由 test_auth_api / test_startup_hook 覆盖，端到端验证走 smoke 脚本
- systemd 单元、Docker Redis、安全组等部署项属运维验收范围
