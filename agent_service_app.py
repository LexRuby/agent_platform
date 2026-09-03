"""生产部署形态 B：AgentScope 官方 agent-service（多租户/多会话）+ Web UI。

- 存储：Redis（docker: agentforge-redis，:6379）
- 消息总线：进程内（单 worker 部署；多进程时换 RedisMessageBus）
- MCP：不在工作区默认注入任何 MCP；需要时在 UI 的「MCP」页显式添加
- Web UI：官方 examples/web_ui 构建产物挂载在同源 "/"（零配置，访问 :30000 即用）
- 用户管理：文件驱动（data/users/<用户名>.txt = 明文密码）+ Redis 会话 +
  ASGI 鉴权中间件（未登录 401/302 /login，伪造 X-User-ID 无效）
- 提示词模板：prompt_templates/*.yaml，创建 Agent 时可选（/prompt-templates）

启动前置：Redis 运行中。
模型凭据在 UI 的「凭据」页配置（豆包 = OpenAI 兼容：ARK key + base_url + 模型名）。
"""

import os
from pathlib import Path

import uvicorn
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware

from agentscope.app import create_app
from agentscope.app.hub import GitHubMCPHub
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.rag.knowledge_base_manager import CollectionPerKbManager
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.rag import ApproxTokenChunker, QdrantStore
from fastapi.responses import HTMLResponse

from app.agent_type import AgentTypeMiddleware
from app.agent_version import AgentVersionMiddleware, agent_version_router
from app.auth import AuthMiddleware, _LOGIN_HTML, auth_router
from app.ark_credential import ArkCredential
from app.leader_team import LeaderTeamMiddleware, leader_team_router
from app.local_skill_hub import LocalSkillHub
from app.prompt_templates import (
    PromptTemplateSchemaMiddleware,
    prompt_templates_router,
)
from app.spa_static import SPAStaticFiles
from app.startup_hook import StartupHook
from app.team_archive import team_archive_router

BASE_DIR = Path(__file__).resolve().parent

redis_host = os.environ.get("AGENTFORGE_REDIS_HOST", "localhost")
redis_port = int(os.environ.get("AGENTFORGE_REDIS_PORT", "6379"))

storage = RedisStorage(host=redis_host, port=redis_port)
vector_store = QdrantStore(location=":memory:")

app = create_app(
    storage=storage,
    message_bus=InMemoryMessageBus(),
    workspace_manager=LocalWorkspaceManager(
        basedir=str(BASE_DIR / "data" / "workspaces"),
    ),
    knowledge_base_manager=CollectionPerKbManager(
        storage=storage,
        vector_store=vector_store,
    ),
    knowledge_chunkers=[ApproxTokenChunker],
    mcp_hubs=[GitHubMCPHub()],
    # 本地技能中心：skill_registry/ 目录（专利检索、智能写作），
    # 技能中心页安装 → 会话工作区装配 SKILL.md 全自动可用
    skill_hubs=[LocalSkillHub(BASE_DIR / "skill_registry")],
    extra_middlewares=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        ),
    ],
    extra_credentials=[ArkCredential],
)

# 用户管理：认证 API + 登录页（在静态挂载之前注册，确保路由优先匹配）
app.include_router(auth_router)

# 官方 app 原始引用：team_archive 等叠加层进程内调用官方端点用
# （不带我们的中间件包装，避免循环鉴权；身份走 X-User-ID 头）
_official_app = app

# 主理人预置团队：member 推荐 API（中间件见下方包装链）
app.include_router(leader_team_router)

# 团队封档：归档草稿/确认/列表 API
app.include_router(team_archive_router)

# agent 版本封板：freeze/unfreeze/save-version/restore API
app.include_router(agent_version_router)

# 提示词模板：列表 API + 注入 /agent/schema/v2（前端据此渲染模板下拉）
app.include_router(prompt_templates_router)

# MCP 工具清单查看：点开「我的 MCP」看内部工具/参数（GET /mcp-tools/{id}）
from app.mcp_tools import init_mcp_tools, mcp_tools_router  # noqa: E402

init_mcp_tools(storage)
app.include_router(mcp_tools_router)


def _start_ark_heartbeat() -> None:
    """lifespan startup 完成后启动 ARK 模型心跳（见 app/startup_hook.py）。"""
    from app.ark_credential import start_heartbeat

    start_heartbeat()


def _StartupHook(inner):
    return StartupHook(inner, _start_ark_heartbeat)


@app.get("/login", include_in_schema=False)
async def login_page() -> HTMLResponse:
    return HTMLResponse(_LOGIN_HTML)


# 官方 Web UI 构建产物（scripts/build_webui.sh 生成），同源挂载免配置。
# SPAStaticFiles：深链接（如 /chat/<agent>/<session>）刷新时回退 index.html，
# 由前端路由接管，不再 404。
_web_dir = BASE_DIR / "webui"
if (_web_dir / "index.html").exists():
    app.mount(
        "/",
        SPAStaticFiles(directory=str(_web_dir), html=True),
        name="webui",
    )

# 最外层：鉴权中间件（cookie 会话 → 身份注入；未登录 401 / 页面 302 /login）
#         + 提示词模板/agent_type 注入 /agent/schema/v2
#         + agent 版本封板（冻结中 PATCH 403 拦截、GET 注入 version 状态；
#           放最外层保证冻结拦截先于 agent_type/leader_team 处理，零副作用）
#         + agent_type 叠加（POST/PATCH 捕获、GET 注入、DELETE 清理）
#         + leader 预置团队叠加（team_members 捕获/提示词注入/GET 注入）
#         + startup 钩子（lifespan 完成后启动 ARK 模型心跳）
from app.spa_static import SPAPageFallbackMiddleware  # noqa: E402

# SPAPageFallbackMiddleware（鉴权内层）：浏览器刷新 /mcp、/skill 等与
# API 路径冲突的前端页面时返回 index.html 而不是 JSON；未登录仍先跳 /login
app = _StartupHook(
    AuthMiddleware(
        SPAPageFallbackMiddleware(
            PromptTemplateSchemaMiddleware(
                AgentVersionMiddleware(
                    LeaderTeamMiddleware(AgentTypeMiddleware(app)),
                ),
            ),
            index_path=str(_web_dir / "index.html"),
        ),
    ),
)

if __name__ == "__main__":
    # 端口从环境变量读取（.env AGENTFORGE_SVC_PORT），默认 30000（对外开放段）
    port = int(os.environ.get("AGENTFORGE_SVC_PORT", "30000"))
    uvicorn.run("agent_service_app:app", host="0.0.0.0", port=port, reload=False)
