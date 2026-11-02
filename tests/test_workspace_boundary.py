"""Workspace boundary regression tests.

The production backend is intentionally local, so this layer is the security
boundary between a browser account and the host filesystem.
"""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode

import pytest

from app.workspace_boundary import (
    WorkspaceBoundaryMiddleware,
    _relative_path,
    patch_chat_first_workspace_instructions,
)


class _WorkspaceService:
    def __init__(self, root):
        self.root = str(root)
        self.calls = []

    async def resolve(self, user_id, agent_id, session_id):
        self.calls.append((user_id, agent_id, session_id))
        return SimpleNamespace(workdir=self.root)


def _app(tmp_path):
    service = _WorkspaceService(tmp_path)

    async def inner(scope, receive, send):
        query = parse_qs(scope.get("query_string", b"").decode())
        request_body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            request_body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        if scope["path"] == "/workspace/directories":
            body = json.dumps({
                "path": str(tmp_path / query.get("path", [""])[0]),
                "entries": [],
            }).encode()
        elif scope["path"] == "/workspace/status":
            body = json.dumps({
                "workdir": str(tmp_path),
                "cwd": str(tmp_path / "drafts"),
                "git": {"branch": "main", "insertions": 638},
            }).encode()
        else:
            body = request_body or b"{}"
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return WorkspaceBoundaryMiddleware(inner, service), service


def _headers():
    return {"X-User-ID": "alice"}


def _params(path=""):
    return {"agent_id": "agent-1", "session_id": "session-1", "path": path}


async def _request(app, method, path, *, params=None, body=None):
    """Drive the middleware directly with a finite ASGI request."""
    messages = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if request_sent:
            return {"type": "http.disconnect"}
        request_sent = True
        return {
            "type": "http.request",
            "body": json.dumps(body).encode() if body is not None else b"",
            "more_body": False,
        }

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": urlencode(params or {}).encode(),
            "headers": [(b"x-user-id", b"alice")],
        },
        receive,
        send,
    )
    start = next(message for message in messages if message["type"] == "http.response.start")
    raw = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(raw), raw.decode()


def test_relative_path_normalizes_inside_project(tmp_path):
    assert _relative_path(str(tmp_path), "drafts/../claims") == "claims"
    assert _relative_path(str(tmp_path), "") == ""


@pytest.mark.parametrize("path", ["/etc", "../other", "drafts/../../other"])
def test_relative_path_rejects_host_escape(tmp_path, path):
    with pytest.raises(ValueError):
        _relative_path(str(tmp_path), path)


def test_relative_path_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        _relative_path(str(tmp_path), "link")


@pytest.mark.asyncio
async def test_directory_listing_returns_only_relative_path(tmp_path):
    app, service = _app(tmp_path)
    status, payload, text = await _request(
        app, "GET", "/workspace/directories",
        params=_params("drafts/../claims"),
    )
    assert status == 200
    assert payload == {"path": "claims", "entries": []}
    assert service.calls == [("alice", "agent-1", "session-1")]
    assert str(tmp_path) not in text


@pytest.mark.asyncio
async def test_directory_listing_blocks_absolute_path(tmp_path):
    app, _ = _app(tmp_path)
    status, payload, _ = await _request(
        app, "GET", "/workspace/directories", params=_params("/etc"),
    )
    assert status == 403
    assert "当前项目空间" in payload["detail"]


@pytest.mark.asyncio
async def test_session_cwd_is_normalized_and_escape_is_rejected(tmp_path):
    app, _ = _app(tmp_path)
    params = {"agent_id": "agent-1", "session_id": "session-1"}

    status, payload, _ = await _request(
        app, "PATCH", "/sessions/session-1", params=params,
        body={"cwd": "drafts/../claims"},
    )
    assert status == 200
    assert payload["cwd"] == "claims"

    status, payload, _ = await _request(
        app, "PATCH", "/sessions/session-1", params=params,
        body={"cwd": "../../etc"},
    )
    assert status == 403


@pytest.mark.asyncio
async def test_status_hides_host_path_and_git_metadata(tmp_path):
    app, _ = _app(tmp_path)
    status, payload, text = await _request(
        app, "GET", "/workspace/status",
        params={"agent_id": "agent-1", "session_id": "session-1"},
    )
    assert status == 200
    assert payload == {"workdir": "", "cwd": "drafts", "git": None}
    assert str(tmp_path) not in text


@pytest.mark.asyncio
async def test_server_path_skill_install_is_disabled(tmp_path):
    app, _ = _app(tmp_path)
    status, payload, _ = await _request(
        app, "POST", "/workspace/skill",
        params={"agent_id": "agent-1", "session_id": "session-1"},
        body={"skill_path": "/etc"},
    )
    assert status == 410
    assert "上传技能文件夹" in payload["detail"]


@pytest.mark.asyncio
async def test_workspace_instructions_authorize_current_space_files(tmp_path):
    from agentscope.workspace import LocalWorkspace

    patch_chat_first_workspace_instructions()
    workspace = LocalWorkspace(workdir=str(tmp_path))
    instructions = await workspace.get_instructions()
    assert "already authorized" in instructions
    assert "temporary and are deleted with the conversation" in instructions
    assert "README" in instructions and "specifically asks" in instructions
    assert "Create a dedicated subdirectory for each task" not in instructions
