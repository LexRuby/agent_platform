"""Agent Forge 发布物 MCP 的认证、授权、运行隔离与公开轨迹测试。"""

import asyncio
import json
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth as auth_mod
from app.agent_share_mcp import (
    _MAX_MESSAGES,
    _message_window,
    _messages,
    agent_share_mcp_router,
)
from app.auth import AuthMiddleware, MCP_TOKEN_PREFIX, auth_router
from tests.test_agent_share import _make_agent


class MCPStorage:
    def __init__(self):
        self._client = fakeredis.FakeAsyncRedis(decode_responses=True)
        self.agents = {("alice", "pub1"): _make_agent("alice", "pub1", "产品")}
        self.sessions = {}
        self.messages = {}
        self.teams = []

    async def get_agent(self, user, agent_id):
        return self.agents.get((user, agent_id))

    async def get_session(self, user, agent_id, session_id):
        rec = self.sessions.get((user, session_id))
        return rec if rec and rec.agent_id == agent_id else None

    async def list_messages(self, user, session_id, limit=200, before=None):
        messages = self.messages.get((user, session_id), [])
        end = len(messages)
        if before is not None:
            end = next(
                (i for i, message in enumerate(messages) if message["id"] == before),
                0,
            )
        start = max(0, end - limit)
        return messages[start:end], start > 0

    async def list_teams(self, user):
        return [t for t in self.teams if t.user_id == user]


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    users = tmp_path / "users"
    users.mkdir()
    for user in ("alice", "bob", "carol"):
        (users / f"{user}.txt").write_text("pw", encoding="utf-8")
    monkeypatch.setattr(auth_mod, "USERS_DIR", users)
    storage = MCPStorage()
    asyncio.run(storage._client.set(
        "agentforge:share:pubmeta:pub1",
        json.dumps({
            "source_agent_id": "source", "source_version": 2,
            "team_mode": "blueprint", "display_name": "产品",
        }),
    ))
    monkeypatch.setattr(auth_mod, "_pool", storage._client)
    app = FastAPI()
    app.state.storage = storage
    app.include_router(auth_router)
    app.include_router(agent_share_mcp_router)
    return storage, TestClient(AuthMiddleware(app)), monkeypatch


def _rpc(client, user, method, params=None, published="pub1", password="pw"):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(
        f"/agent-share/mcp/{published}", json=body, auth=(user, password),
    )


def _share(storage, mode="public", users=None):
    asyncio.run(storage._client.set(
        "agentforge:share:agent:pub1",
        json.dumps({"owner": "alice", "mode": mode, "users": users or []}),
    ))


class TestPublishedAgentMCP:
    def test_tools_list_and_authentication(self, mcp_env):
        storage, client, _ = mcp_env
        _share(storage)
        assert client.post(
            "/agent-share/mcp/pub1",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ).status_code == 401
        assert _rpc(client, "bob", "tools/list", password="bad").status_code == 401
        response = _rpc(client, "bob", "tools/list")
        assert response.status_code == 200
        assert [t["name"] for t in response.json()["result"]["tools"]] == [
            "run_task", "get_run",
        ]

    @pytest.mark.parametrize(
        ("mode", "users", "caller", "allowed"),
        [
            ("users", ["bob"], "alice", True),
            ("public", [], "bob", True),
            ("users", ["bob"], "bob", True),
            ("users", ["bob"], "carol", False),
        ],
    )
    def test_owner_public_and_targeted_policy(
        self, mcp_env, mode, users, caller, allowed,
    ):
        storage, client, _ = mcp_env
        _share(storage, mode, users)
        response = _rpc(client, caller, "tools/list")
        assert response.status_code == (200 if allowed else 404)

    def test_run_task_started_and_get_run_isolated(self, mcp_env):
        storage, client, monkeypatch = mcp_env
        _share(storage)
        asyncio.run(storage._client.set(
            "agentscope:user:bob:session:model-source",
            json.dumps({
                "created_at": "2026-09-09T00:00:00",
                "config": {"chat_model_config": {
                    "type": "openai", "credential_id": "cred", "model": "m",
                    "parameters": {},
                }},
            }),
        ))

        async def official(method, path, user, **kwargs):
            if path == "/sessions/":
                config = SimpleNamespace(chat_model_config=kwargs["json"]["chat_model_config"])
                storage.sessions[(user, "session1")] = SimpleNamespace(
                    id="session1", agent_id="pub1", config=config,
                )
                return SimpleNamespace(status_code=201, json=lambda: {"session_id": "session1"})
            if path == "/chat/":
                message = kwargs["json"]["input"]
                message["created_at"] = "2026-09-09T00:00:01+00:00"
                storage.messages.setdefault((user, "session1"), []).append(message)
                return SimpleNamespace(status_code=200, json=lambda: {"status": "started"})
            return SimpleNamespace(status_code=200, json=lambda: {"status": "idle"})

        monkeypatch.setattr("app.agent_share_mcp._official", official)
        first = _rpc(client, "bob", "tools/call", {
            "name": "run_task", "arguments": {"task": "第一次"},
        })
        first_run = json.loads(first.json()["result"]["content"][0]["text"])
        storage.messages[("bob", "session1")].append({
            "id": "answer-1", "role": "assistant", "name": "产品",
            "created_at": "2026-09-09T00:00:02+00:00",
            "finished_at": "2026-09-09T00:00:03+00:00", "finished_reason": "stop",
            "content": [{"type": "text", "text": "第一段答案"}],
        })
        second = _rpc(client, "bob", "tools/call", {
            "name": "run_task", "arguments": {
                "task": "第二次", "session_id": "session1",
            },
        })
        second_run = json.loads(second.json()["result"]["content"][0]["text"])
        storage.messages[("bob", "session1")].append({
            "id": "answer-2", "role": "assistant", "name": "产品",
            "created_at": "2026-09-09T00:00:04+00:00",
            "finished_at": "2026-09-09T00:00:05+00:00", "finished_reason": "completed",
            "content": [{"type": "text", "text": "第二段答案"}],
        })

        assert first_run["run_id"] != second_run["run_id"]
        assert first_run["session_id"] == second_run["session_id"] == "session1"
        assert first_run["reused_session"] is False
        assert second_run["reused_session"] is True
        for run in (first_run, second_run):
            key = f"agentforge:share:mcp-run:bob:pub1:{run['run_id']}"
            assert asyncio.run(storage._client.exists(key)) == 1

        first_result = _rpc(client, "bob", "tools/call", {
            "name": "get_run", "arguments": {"run_id": first_run["run_id"]},
        })
        first_payload = json.loads(first_result.json()["result"]["content"][0]["text"])
        second_result = _rpc(client, "bob", "tools/call", {
            "name": "get_run", "arguments": {"run_id": second_run["run_id"]},
        })
        second_payload = json.loads(second_result.json()["result"]["content"][0]["text"])
        assert first_payload["task"] == "第一次"
        assert first_payload["trajectory"][0]["evidence_id"] == first_run["start_message_id"]
        assert second_payload["final_answer"] == "第二段答案"
        assert [event["evidence_id"] for event in second_payload["trajectory"]] == [
            second_run["start_message_id"], "answer-2",
        ]

        denied = _rpc(client, "carol", "tools/call", {
            "name": "get_run", "arguments": {"run_id": first_run["run_id"]},
        })
        assert denied.json()["result"]["isError"] is True

    @pytest.mark.parametrize(
        ("leader_status", "member_status", "aggregate_status"),
        [
            ("idle", "running", "running"),
            ("idle", "unknown", "idle"),
            ("unknown", "idle", "idle"),
        ],
    )
    def test_get_run_filters_thinking_redacts_and_aggregates_team(
        self, mcp_env, leader_status, member_status, aggregate_status,
    ):
        storage, client, monkeypatch = mcp_env
        _share(storage, "users", ["bob"])
        config = SimpleNamespace(chat_model_config={"model": "m"})
        storage.sessions[("bob", "run1")] = SimpleNamespace(
            id="run1", agent_id="pub1", config=config,
        )
        storage.sessions[("bob", "member-sid")] = SimpleNamespace(
            id="member-sid", agent_id="member", config=config,
        )
        storage.agents[("bob", "member")] = SimpleNamespace(data=SimpleNamespace(
            name="研究员", invite_config=None,
            system_prompt=(
                "You are a team member.\n\n"
                "Your role: 查证事实并整理证据\n覆盖多行职责\n\n"
                "You communicate with the leader."
            ),
        ))
        storage.teams = [SimpleNamespace(
            user_id="bob", session_id="run1",
            data=SimpleNamespace(members=[SimpleNamespace(
                owner_id="bob", agent_id="member", session_id="member-sid",
                role="created",
            )]),
        )]
        storage.messages[("bob", "run1")] = [{
            "id": "leader-start", "role": "user", "name": "user",
            "created_at": "2026-09-09T00:00:00+00:00",
            "content": [{"type": "text", "text": "分析"}],
        }, {
            "id": "leader-1", "role": "assistant", "name": "产品",
            "created_at": "2026-09-09T00:00:02+00:00",
            "finished_at": "2026-09-09T00:00:03+00:00", "finished_reason": "stop",
            "content": [
                {"type": "thinking", "thinking": "隐式思维链"},
                {"type": "tool_call", "id": "tc", "name": "search",
                 "input": json.dumps({"api_key": "secret", "query": "公开"})},
                {"type": "text", "text": "最终答案"},
            ],
        }]
        storage.messages[("bob", "member-sid")] = [{
            "id": "member-old", "role": "assistant", "name": "研究员",
            "created_at": "2026-09-08T23:59:59+00:00",
            "content": [{"type": "text", "text": "历史成员消息"}],
        }, {
            "id": "member-1", "role": "assistant", "name": "研究员",
            "created_at": "2026-09-09T00:00:01+00:00",
            "content": [{"type": "tool_result", "id": "tr", "name": "search",
                         "output": {"password": "p", "fact": "依据"}}],
        }, {
            "id": "member-2", "role": "assistant", "name": "研究员",
            "created_at": "2026-09-09T00:00:01+00:00",
            "content": [{"type": "text", "text": "同时间后续证据"}],
        }]
        asyncio.run(storage._client.set(
            "agentforge:share:mcp-run:bob:pub1:run1",
            json.dumps({
                "run_id": "run1", "session_id": "run1", "task": "分析",
                "start_message_id": "leader-start",
                "created_at": "2026-09-09T00:00:00+00:00",
                "reused_session": True,
            }),
        ))

        async def official(method, path, user, **kwargs):
            assert method == "GET" and user == "bob"
            assert kwargs["params"]["agent_id"] in {"pub1", "member"}
            status = (
                member_status
                if path == "/sessions/member-sid/status"
                else leader_status
            )
            return SimpleNamespace(status_code=200, json=lambda: {"status": status})
        monkeypatch.setattr("app.agent_share_mcp._official", official)
        response = _rpc(client, "bob", "tools/call", {
            "name": "get_run", "arguments": {"run_id": "run1"},
        })
        payload = json.loads(response.json()["result"]["content"][0]["text"])
        encoded = json.dumps(payload, ensure_ascii=False)
        assert "隐式思维链" not in encoded and "thinking" not in encoded
        assert "历史成员消息" not in encoded
        assert "secret" not in encoded and '"password": "[REDACTED]"' in encoded
        assert '\\"api_key\\": \\"[REDACTED]\\"' in encoded
        assert payload["final_answer"] == "最终答案"
        assert payload["status"] == aggregate_status
        assert payload["completed"] is False
        assert payload["team"]["leader"]["status"] == leader_status
        assert payload["team"]["members"][0]["status"] == member_status
        assert payload["team"]["members"][0]["responsibility"] == (
            "查证事实并整理证据\n覆盖多行职责"
        )
        assert [e["evidence_id"] for e in payload["trajectory"]] == [
            "leader-start", "member-1", "member-2", "leader-1",
        ]
        assert all(e["source"] == "observed" for e in payload["trajectory"])
        assert {e["session_id"] for e in payload["trajectory"]} == {"run1", "member-sid"}

    def test_messages_paginates_to_run_start(self):
        storage = MCPStorage()
        storage.messages[("bob", "long")] = [
            {
                "id": f"m{i}", "role": "assistant",
                "created_at": f"2026-09-09T00:{i // 60:02d}:{i % 60:02d}+00:00",
                "content": [{"type": "text", "text": str(i)}],
            }
            for i in range(450)
        ]
        messages, truncated = asyncio.run(_messages(storage, "bob", "long"))
        window = _message_window(messages, "m100", "")
        assert truncated is False
        assert [message["id"] for message in window] == [
            f"m{i}" for i in range(100, 450)
        ]

    def test_messages_reports_cap_truncation(self):
        storage = MCPStorage()
        storage.messages[("bob", "long")] = [
            {"id": f"m{i}"} for i in range(_MAX_MESSAGES + 1)
        ]
        messages, truncated = asyncio.run(_messages(storage, "bob", "long"))
        assert len(messages) == _MAX_MESSAGES
        assert messages[0]["id"] == "m1"
        assert truncated is True

    def test_browser_get_shows_usage_guide(self, mcp_env):
        storage, client, _ = mcp_env
        _share(storage, "public")
        response = client.get(
            "/agent-share/mcp/pub1", auth=("bob", "pw"),
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "Agent Forge MCP" in response.text
        assert "run_task" in response.text
        assert "get_run" in response.text
        assert "/agent-share/mcp/pub1" in response.text

    def test_browser_get_requires_access(self, mcp_env):
        storage, client, _ = mcp_env
        _share(storage, "users", ["bob"])
        assert client.get(
            "/agent-share/mcp/pub1", auth=("carol", "pw"),
        ).status_code == 404

    def test_scoped_bearer_config_and_revocation(self, mcp_env):
        storage, client, _ = mcp_env
        _share(storage, "users", ["bob"])
        assert client.post(
            "/agent-share/mcp/pub1/config",
            headers={"X-User-ID": "bob"},
        ).status_code == 401
        assert client.post(
            "/agent-share/mcp/pub1/config", auth=("bob", "pw"),
        ).status_code == 401

        assert client.post(
            "/auth/login", json={"username": "bob", "password": "pw"},
        ).status_code == 200
        issued = client.post("/agent-share/mcp/pub1/config")
        assert issued.status_code == 200
        config = issued.json()["config"]
        name, server = next(iter(config["mcpServers"].items()))
        assert name == "产品"
        assert server["url"].endswith("/agent-share/mcp/pub1")
        authorization = server["headers"]["Authorization"]
        assert authorization.startswith("Bearer af_mcp_")
        token = authorization.removeprefix("Bearer ")
        assert token not in json.dumps(asyncio.run(storage._client.keys("*")))
        assert len(asyncio.run(storage._client.keys(MCP_TOKEN_PREFIX + "*"))) == 1

        rpc = client.post(
            "/agent-share/mcp/pub1",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": authorization},
        )
        assert rpc.status_code == 200
        assert client.post(
            "/agent-share/mcp/other",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": authorization},
        ).status_code == 401
        # 清掉登录 Cookie 后只带 Bearer，凭证不得用于管理接口。
        client.cookies.clear()
        assert client.get(
            "/auth/mcp-tokens/status",
            headers={"Authorization": authorization},
        ).status_code == 401
        assert client.post(
            "/auth/login", json={"username": "bob", "password": "pw"},
        ).status_code == 200
        assert client.get("/auth/mcp-tokens/status").json() == {"active_count": 1}
        assert client.delete("/auth/mcp-tokens").json()["revoked_count"] == 1
        assert client.post(
            "/agent-share/mcp/pub1",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": authorization},
        ).status_code == 401

    def test_config_requires_publication_access(self, mcp_env):
        storage, client, _ = mcp_env
        _share(storage, "users", ["bob"])
        assert client.post(
            "/auth/login", json={"username": "carol", "password": "pw"},
        ).status_code == 200
        assert client.post("/agent-share/mcp/pub1/config").status_code == 404

    def test_deleted_publication_rejected(self, mcp_env):
        storage, client, _ = mcp_env
        _share(storage)
        asyncio.run(storage._client.delete("agentforge:share:pubmeta:pub1"))
        assert _rpc(client, "bob", "tools/list").status_code == 404
