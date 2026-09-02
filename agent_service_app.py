"""生产部署形态：AgentScope 官方 agent-service（多租户/多会话/分布式）。

工单服务（app/main.py）适合 W1 演示；上生产时改用本入口，
配合 examples/web_ui 前端即可获得多租户聊天服务。
依赖 Redis：`docker run -d -p 6379:6379 redis`
"""

import os

import uvicorn
from agentscope.app import create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

storage = RedisStorage(
    host=os.environ.get("AGENTFORGE_REDIS_HOST", "localhost"),
    port=int(os.environ.get("AGENTFORGE_REDIS_PORT", "6379")),
)
message_bus = RedisMessageBus(
    host=os.environ.get("AGENTFORGE_REDIS_HOST", "localhost"),
    port=int(os.environ.get("AGENTFORGE_REDIS_PORT", "6379")),
)
workspace_manager = LocalWorkspaceManager(
    basedir="/data/workspaces",
    ttl=3600.0,
)

app = create_app(
    storage=storage,
    message_bus=message_bus,
    workspace_manager=workspace_manager,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
