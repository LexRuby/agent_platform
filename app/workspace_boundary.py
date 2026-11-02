"""Hard workspace boundary for the local AgentForge deployment.

AgentScope's upstream local-workspace endpoints intentionally accept absolute
paths and paths outside the workspace root.  That assumption is unsuitable for
this multi-user service: a browser user must never be able to browse or select
an arbitrary server directory.  This ASGI middleware keeps every user supplied
path relative to the resolved session workspace and removes host paths and Git
metadata from workspace responses.
"""

from __future__ import annotations

import json
import os
from urllib.parse import parse_qsl, urlencode


CHAT_FIRST_WORKSPACE_INSTRUCTIONS = """<workspace>
You have a private writable workspace for this conversation or project.

Default behavior:
- You are already authorized to read, create, edit, move, and delete files
  inside this workspace. Do not ask for permission for these routine file
  operations.
- Create deliverable files whenever they are useful or requested. In a normal
  conversation they are temporary and are deleted with the conversation. In
  a project they remain in the project workspace.
- Work directly in the current root. Do not create a date-prefixed task
  directory or README unless the user specifically asks for one.
- Never reveal or attempt to access the host path behind the project space.
</workspace>"""


def patch_chat_first_workspace_instructions() -> None:
    """Replace AgentScope's eager project/README policy for local workspaces."""
    from agentscope.workspace import LocalWorkspace

    if getattr(LocalWorkspace, "_agentforge_chat_first", False):
        return

    async def get_instructions(self) -> str:
        return CHAT_FIRST_WORKSPACE_INSTRUCTIONS

    LocalWorkspace.get_instructions = get_instructions
    LocalWorkspace._agentforge_chat_first = True

def _read_user_id(scope: dict) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == b"x-user-id":
            return value.decode("utf-8", errors="strict")
    return None


def _query(scope: dict) -> dict[str, str]:
    return dict(parse_qsl(scope.get("query_string", b"").decode("utf-8"),
                          keep_blank_values=True))


def _replace_query(scope: dict, values: dict[str, str]) -> dict:
    updated = dict(scope)
    updated["query_string"] = urlencode(values).encode("utf-8")
    return updated


async def _read_body(receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            continue
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


def _body_receive(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _relative_path(root: str, value: str | None) -> str:
    """Return a normalized workspace-relative path or raise ValueError."""
    raw = (value or "").strip()
    if "\x00" in raw or os.path.isabs(raw):
        raise ValueError("工作目录必须位于当前项目空间内")

    normalized = os.path.normpath(raw or ".")
    if normalized == ".":
        normalized = ""
    candidate = os.path.realpath(os.path.join(root, normalized))
    real_root = os.path.realpath(root)
    try:
        inside = os.path.commonpath([real_root, candidate]) == real_root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("工作目录不能离开当前项目空间")
    return normalized


async def _json_error(send, status_code: int, detail: str) -> None:
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})


class WorkspaceBoundaryMiddleware:
    """Confine workspace paths and suppress host repository metadata."""

    def __init__(self, app, workspace_service) -> None:
        self.app = app
        self.workspace_service = workspace_service

    async def _root(self, scope: dict, query: dict[str, str]) -> str:
        user_id = _read_user_id(scope)
        agent_id = query.get("agent_id")
        session_id = query.get("session_id")
        if not user_id or not agent_id or not session_id:
            raise ValueError("缺少工作空间身份信息")
        service = (
            self.workspace_service()
            if callable(self.workspace_service)
            else self.workspace_service
        )
        workspace = await service.resolve(
            user_id,
            agent_id,
            session_id,
        )
        return workspace.workdir

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()

        # The deprecated endpoint copies from an arbitrary server-side path.
        # Folder upload and the skill library remain available.
        if path == "/workspace/skill" and method == "POST":
            await _json_error(send, 410, "服务器路径安装已禁用，请上传技能文件夹或从技能库安装")
            return

        query_paths = {
            ("/workspace/directories", "GET"),
            ("/workspace/files", "GET"),
            ("/workspace/files/download-token", "POST"),
        }
        response_kind: str | None = None
        normalized_path = ""
        root: str | None = None

        if (path, method) in query_paths:
            values = _query(scope)
            try:
                root = await self._root(scope, values)
                normalized_path = _relative_path(root, values.get("path"))
            except ValueError as exc:
                await _json_error(send, 403, str(exc))
                return
            values["path"] = normalized_path
            scope = _replace_query(scope, values)
            if path == "/workspace/directories":
                response_kind = "directories"

        elif path == "/workspace/status" and method == "GET":
            values = _query(scope)
            try:
                root = await self._root(scope, values)
            except ValueError as exc:
                await _json_error(send, 403, str(exc))
                return
            response_kind = "status"

        elif path.startswith("/sessions/") and method == "PATCH":
            body = await _read_body(receive)
            try:
                payload = json.loads(body or b"{}")
            except (TypeError, ValueError):
                await self.app(scope, _body_receive(body), send)
                return
            if isinstance(payload, dict) and payload.get("cwd") is not None:
                values = _query(scope)
                try:
                    root = await self._root(scope, values)
                    payload["cwd"] = _relative_path(root, payload["cwd"]) or None
                except ValueError as exc:
                    await _json_error(send, 403, str(exc))
                    return
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            receive = _body_receive(body)

        if response_kind is None:
            await self.app(scope, receive, send)
            return

        messages: list[dict] = []

        async def capture(message: dict) -> None:
            messages.append(message)

        await self.app(scope, receive, capture)
        start = next((m for m in messages if m["type"] == "http.response.start"), None)
        bodies = [m for m in messages if m["type"] == "http.response.body"]
        if start is None or start.get("status") != 200 or not bodies:
            for message in messages:
                await send(message)
            return

        raw_body = b"".join(m.get("body", b"") for m in bodies)
        try:
            payload = json.loads(raw_body)
            if response_kind == "directories" and isinstance(payload, dict):
                payload["path"] = normalized_path
            elif response_kind == "status" and isinstance(payload, dict):
                cwd = payload.get("cwd")
                payload = {
                    "workdir": "",
                    "cwd": _relative_path(root or "", os.path.relpath(cwd, root))
                    if isinstance(cwd, str) and root
                    else "",
                    "git": None,
                }
            rewritten = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError, OSError):
            for message in messages:
                await send(message)
            return

        headers = [
            (key, value)
            for key, value in start.get("headers", [])
            if key.lower() != b"content-length"
        ]
        headers.append((b"content-length", str(len(rewritten)).encode("ascii")))
        await send({**start, "headers": headers})
        await send({
            "type": "http.response.body",
            "body": rewritten,
            "more_body": False,
        })
