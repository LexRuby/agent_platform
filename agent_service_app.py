"""生产部署形态 B：AgentScope 官方 agent-service（多租户/多会话）+ Web UI。

- 存储：Redis（docker: agentforge-redis，:6379）
- 消息总线：进程内（单 worker 部署；多进程时换 RedisMessageBus）
- 工作区默认 MCP：中台能力（scripts/midplatform_mcp.py，:9200）
- Web UI：官方 examples/web_ui 构建产物挂载在同源 "/"（零配置，访问 :8300 即用）

启动前置：Redis 运行中 + scripts/midplatform_mcp.py 运行中。
模型凭据在 UI 的「凭据」页配置（豆包 = OpenAI 兼容：ARK key + base_url + 模型名）。
"""

import os
from pathlib import Path

import uvicorn
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from agentscope.app import create_app
from agentscope.app.hub import GitHubMCPHub
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.rag.knowledge_base_manager import CollectionPerKbManager
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.mcp import HttpMCPConfig, MCPClient
from agentscope.rag import ApproxTokenChunker, QdrantStore

BASE_DIR = Path(__file__).resolve().parent

redis_host = os.environ.get("AGENTFORGE_REDIS_HOST", "localhost")
redis_port = int(os.environ.get("AGENTFORGE_REDIS_PORT", "6379"))

storage = RedisStorage(host=redis_host, port=redis_port)
vector_store = QdrantStore(location=":memory:")

# 工作区默认接入中台能力 MCP（检索/写作，后续补标注/可视化）
default_mcps = [
    MCPClient(
        name="midplatform",
        mcp_config=HttpMCPConfig(
            url=os.environ.get(
                "MIDPLATFORM_MCP_URL", "http://127.0.0.1:9200/mcp/",
            ),
        ),
        is_stateful=False,
    ),
]

app = create_app(
    storage=storage,
    message_bus=InMemoryMessageBus(),
    workspace_manager=LocalWorkspaceManager(
        basedir=str(BASE_DIR / "data" / "workspaces"),
        default_mcps=default_mcps,
    ),
    knowledge_base_manager=CollectionPerKbManager(
        storage=storage,
        vector_store=vector_store,
    ),
    knowledge_chunkers=[ApproxTokenChunker],
    mcp_hubs=[GitHubMCPHub()],
    extra_middlewares=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        ),
    ],
)

# 官方 Web UI 构建产物（scripts/build_webui.sh 生成），同源挂载免配置
_web_dir = BASE_DIR / "webui"
if (_web_dir / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="webui")

if __name__ == "__main__":
    uvicorn.run("agent_service_app:app", host="0.0.0.0", port=8300, reload=False)
