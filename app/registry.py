"""控制台注册中心：MCP Server 注册、Agent 定义（draft/locked/published）、工作空间会话。

运行时状态，JSON 持久化到 data/registry.json（data/ 已 gitignore）。
"""

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

AGT_DRAFT = "draft"
AGT_LOCKED = "locked"
AGT_PUBLISHED = "published"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class Registry:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict = {"mcp_servers": {}, "agents": [], "workspaces": {}}
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        if self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    # ---- MCP Server ----
    def add_mcp_server(self, name: str, url: str, token: str = "", description: str = "") -> dict:
        with self._lock:
            self._data["mcp_servers"][name] = {
                "name": name,
                "url": url,
                "token": token,
                "description": description,
                "tools": [],
                "updated_at": _now(),
            }
            self._save()
            return self._data["mcp_servers"][name]

    def remove_mcp_server(self, name: str) -> None:
        with self._lock:
            self._data["mcp_servers"].pop(name, None)
            self._save()

    def get_mcp_server(self, name: str) -> dict | None:
        return self._data["mcp_servers"].get(name)

    def list_mcp_servers(self) -> list[dict]:
        # token 不外泄
        return [
            {k: v for k, v in srv.items() if k != "token"}
            for srv in self._data["mcp_servers"].values()
        ]

    def update_mcp_tools(self, name: str, tools: list[dict]) -> None:
        with self._lock:
            if name in self._data["mcp_servers"]:
                self._data["mcp_servers"][name]["tools"] = tools
                self._data["mcp_servers"][name]["updated_at"] = _now()
                self._save()

    # ---- Agent ----
    def create_agent(
        self, name: str, description: str, system_prompt: str, tools: list[str],
    ) -> dict:
        agent = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "tools": tools,  # 引用形如 "builtin:xxx" / "mcp:server:tool"
            "status": AGT_DRAFT,
            "created_at": _now(),
            "updated_at": _now(),
        }
        with self._lock:
            self._data["agents"].append(agent)
            self._save()
        return agent

    def list_agents(self, status: str | None = None) -> list[dict]:
        return [
            a for a in self._data["agents"] if status is None or a["status"] == status
        ]

    def get_agent(self, agent_id: str) -> dict | None:
        for a in self._data["agents"]:
            if a["id"] == agent_id:
                return a
        return None

    def update_agent(self, agent_id: str, patch: dict) -> dict | None:
        with self._lock:
            for a in self._data["agents"]:
                if a["id"] == agent_id:
                    if a["status"] != AGT_DRAFT:
                        raise PermissionError("仅 draft 状态的 Agent 可编辑，锁定/发布后不可修改")
                    for key in ("name", "description", "system_prompt", "tools"):
                        if key in patch:
                            a[key] = patch[key]
                    a["updated_at"] = _now()
                    self._save()
                    return a
        return None

    def set_agent_status(self, agent_id: str, status: str) -> dict | None:
        with self._lock:
            for a in self._data["agents"]:
                if a["id"] == agent_id:
                    a["status"] = status
                    a["updated_at"] = _now()
                    self._save()
                    return a
        return None

    # ---- 工作空间会话 ----
    def get_workspace(self, agent_id: str) -> dict:
        return self._data["workspaces"].setdefault(agent_id, {"messages": []})

    def append_message(self, agent_id: str, role: str, content: str) -> dict:
        msg = {"role": role, "content": content, "at": _now()}
        with self._lock:
            ws = self.get_workspace(agent_id)
            ws["messages"].append(msg)
            self._save()
        return msg

    def clear_workspace(self, agent_id: str) -> None:
        with self._lock:
            self._data["workspaces"][agent_id] = {"messages": []}
            self._save()
