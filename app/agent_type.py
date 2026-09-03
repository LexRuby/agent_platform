"""Agent 类型（大A / 小A）：在官方 Agent 上叠加的本地分类标注。

三层架构中：
- ``leader``（大A · 主理人）：团队型智能体的领导者，负责规划、组队、分派
- ``member``（小A · 专家）：垂直领域专家，被大A 邀请或临时创建执行任务

官方 ``AgentData`` 没有类型字段，本模块在不改官方代码的前提下叠加：

- :class:`AgentTypeStore`：``data/agent_types.json`` 持久化 ``{agent_id: 类型}``；
  没有映射的 agent 默认 ``member``
- :class:`AgentTypeMiddleware`：纯 ASGI 中间件，包在官方 app 外层——
  - ``POST /agent/``：请求体里的 ``agent_type`` 剥离后存映射（agent_id 取自响应）
  - ``PATCH /agent/{id}``：同上（agent_id 取自路径）
  - ``DELETE /agent/{id}``：成功后清理映射
  - ``GET /agent/``、``GET /agent/{id}``：响应中每个 agent 顶层注入 ``agent_type``
- :func:`inject_agent_type_schema`：把 ``agent_type`` enum 注入
  ``/agent/schema/v2``，官方 Web UI 表单据此渲染下拉框
"""

import json
import logging
import os
from pathlib import Path

_logger = logging.getLogger("agentforge.agent_type")

_BASE_DIR = Path(__file__).resolve().parent.parent
_TYPES_FILE = _BASE_DIR / "data" / "agent_types.json"

LEADER = "leader"
MEMBER = "member"
VALID_TYPES = (LEADER, MEMBER)


def _types_file() -> Path:
    """映射文件路径（环境变量可覆盖，主要供测试隔离）。"""
    return Path(
        os.environ.get("AGENTFORGE_AGENT_TYPES_FILE", str(_TYPES_FILE)),
    )


class AgentTypeStore:
    """agent_id → 类型 的文件持久化。单 worker 部署下读写足够。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else _types_file()

    def load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if v in VALID_TYPES}
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001 - 损坏文件降级为空
            _logger.warning("agent 类型映射文件损坏（按空处理）%s: %s", self.path, e)
            return {}

    def get(self, agent_id: str) -> str:
        return self.load().get(agent_id, MEMBER)

    def set(self, agent_id: str, agent_type: str) -> None:
        if agent_type not in VALID_TYPES:
            raise ValueError(f"非法 agent_type: {agent_type!r}")
        mapping = self.load()
        if mapping.get(agent_id) == agent_type:
            return
        mapping[agent_id] = agent_type
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def delete(self, agent_id: str) -> None:
        mapping = self.load()
        if agent_id not in mapping:
            return
        del mapping[agent_id]
        self.path.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8",
        )


def _default_store() -> AgentTypeStore:
    return AgentTypeStore()


async def _read_body(receive) -> bytes:
    chunks = []
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        chunks.append(msg.get("body", b""))
        if not msg.get("more_body"):
            break
    return b"".join(chunks)


def _make_receive(body: bytes):
    """构造一次性返回给定 body 的 receive。"""
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _extract_agent_type(body: bytes) -> tuple[bytes, str | None]:
    """从 JSON 请求体剥离 agent_type；非 JSON / 无该键返回原样。"""
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001 - 非 JSON 透传
        return body, None
    if not isinstance(data, dict) or "agent_type" not in data:
        return body, None
    agent_type = data.pop("agent_type")
    return json.dumps(data, ensure_ascii=False).encode("utf-8"), agent_type


def _match(path: str, method: str, prefix: str = "/agent") -> dict:
    """匹配 /agent 与 /agent/{id}（容忍尾斜杠）；返回 {"id": str|None}，不匹配返回 {}。"""
    p = path.rstrip("/") or "/"
    if p == prefix:
        return {"id": None} if method in ("POST", "GET") else {}
    if p.startswith(prefix + "/"):
        rest = p[len(prefix) + 1:]
        if not rest or "/" in rest:
            return {}
        return {"id": rest}
    return {}


class AgentTypeMiddleware:
    """在官方 agent API 上叠加 agent_type：请求捕获存储，响应注入。"""

    def __init__(self, app, store: AgentTypeStore | None = None) -> None:
        self.app = app
        self.store = store or _default_store()

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        m = _match(path, method)
        if not m:
            await self.app(scope, receive, send)
            return

        agent_id = m["id"]

        # ── 写路径：POST（body 里的 agent_type + 响应拿 id）/ PATCH / DELETE ──
        if method in ("POST", "PATCH"):
            body = await _read_body(receive)
            new_body, agent_type = _extract_agent_type(body)
            # body 已被消费，必须总是重建 receive 再透传，
            # 否则内层应用再 receive() 会永久挂起
            receive = _make_receive(new_body)
            if agent_type is not None:
                if agent_id is not None:  # PATCH：id 在路径
                    try:
                        self.store.set(agent_id, agent_type)
                    except ValueError as e:
                        _logger.warning("忽略非法 agent_type: %s", e)
                    agent_type = None  # 已处理，不再等响应
                # POST：等响应拿 agent_id 再存
                if agent_type is not None:
                    await self._run_capture_id(
                        scope, receive, send, agent_type,
                    )
                    return
            await self.app(scope, receive, send)
            return

        if method == "DELETE" and agent_id is not None:
            await self._run_delete(scope, receive, send, agent_id)
            return

        # ── 读路径：GET 列表/单个 → 响应注入 agent_type ──
        await self._run_inject(scope, receive, send)
        return

    async def _run_capture_id(self, scope, receive, send, agent_type: str) -> None:
        """POST /agent/：透传请求，响应 200 时从 body 提取 id 存映射。"""
        state = {"start": None, "chunks": []}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                state["start"] = message
                return
            if message["type"] == "http.response.body":
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                status = (state["start"] or {}).get("status", 500)
                if status in (200, 201):
                    try:
                        data = json.loads(b"".join(state["chunks"]))
                        # 官方 POST /agent/ 响应顶层是 agent_id；
                        # 兼容 {id} / {agent:{id}} 两种包装以防上游调整
                        new_id = (
                            data.get("agent_id")
                            or data.get("id")
                            or data.get("agent", {}).get("id")
                        )
                        if new_id:
                            self.store.set(new_id, agent_type)
                    except Exception as e:  # noqa: BLE001 - 存储失败不影响主流程
                        _logger.warning("agent_type 存储失败: %s", e)
                await _replay(state, send)
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _run_delete(self, scope, receive, send, agent_id: str) -> None:
        state = {"start": None, "chunks": []}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                state["start"] = message
                return
            if message["type"] == "http.response.body":
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                status = (state["start"] or {}).get("status", 500)
                if status in (200, 204):
                    self.store.delete(agent_id)
                await _replay(state, send)
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _run_inject(self, scope, receive, send) -> None:
        state = {"start": None, "chunks": []}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                state["start"] = message
                return
            if message["type"] == "http.response.body":
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(state["chunks"])
                status = (state["start"] or {}).get("status", 500)
                if status == 200:
                    body = self._inject_body(body)
                await _replay(state, send, override_body=body)
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _inject_body(self, body: bytes) -> bytes:
        """把 agent_type 写进响应（列表或单个），失败原样返回。"""
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001
            return body
        if isinstance(data, dict) and isinstance(data.get("agents"), list):
            for a in data["agents"]:
                if isinstance(a, dict) and "id" in a:
                    a["agent_type"] = self.store.get(a["id"])
        elif isinstance(data, dict) and "id" in data:
            data["agent_type"] = self.store.get(data["id"])
        else:
            return body
        try:
            return json.dumps(data, ensure_ascii=False).encode("utf-8")
        except Exception as e:  # noqa: BLE001
            _logger.warning("agent_type 注入失败: %s", e)
            return body


async def _replay(state: dict, send, override_body: bytes | None = None) -> None:
    """按原状态重放响应；override_body 时同步修正 content-length。"""
    start = state["start"] or {
        "type": "http.response.start", "status": 500, "headers": [],
    }
    body = override_body if override_body is not None else b"".join(state["chunks"])
    headers = list(start.get("headers", []))
    if override_body is not None:
        headers = [
            (k, v) for k, v in headers if k.decode().lower() != "content-length"
        ]
        headers.append((b"content-length", str(len(body)).encode()))
    await send({
        "type": "http.response.start",
        "status": start.get("status", 500),
        "headers": headers,
    })
    await send({"type": "http.response.body", "body": body})


def agent_type_schema_property() -> dict:
    """agent_type 的 JSON Schema 属性（注入官方 AgentData schema 用）。"""
    return {
        "type": "string",
        "enum": list(VALID_TYPES),
        "title": "Agent 类型",
        "default": MEMBER,
        "description": (
            "leader=大A 主理人（规划任务、组建团队、分派工作）；"
            "member=小A 专家（垂直领域专家，被大A 邀请协作）"
        ),
    }
