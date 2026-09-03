"""team_archive 模块测试：任务归档 → 团队封档。

测试隔离原则：
- 封档文件 → tmp_path（AGENTFORGE_TEAM_ARCHIVES_FILE）
- 官方端点 → monkeypatch _call_official（fake ASGI 响应）
- LLM → monkeypatch app.team_archive.llm_chat_json
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.team_archive as ta
from app.team_archive import (
    ArchiveStore,
    _messages_to_transcript,
    team_archive_router,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_TEAM_ARCHIVES_FILE", str(tmp_path / "archives.json"))
    return tmp_path


@pytest.fixture
def client(env):
    """挂 team_archive_router 的极简 app + X-User-ID 模拟已登录。"""
    app = FastAPI()
    app.include_router(team_archive_router)
    return TestClient(app, headers={"X-User-ID": "tester"})


FAKE_MESSAGES = [
    {
        "role": "user", "name": "user",
        "content": [{"type": "text", "text": "山东考生610分怎么报志愿"}],
    },
    {
        "role": "assistant", "name": "主理人",
        "content": [
            {"type": "tool_call", "name": "TeamCreate", "input": {"name": "专家组"}},
            {"type": "tool_call", "name": "TeamSay",
             "input": {"to": "政策研究员", "content": "请分析政策影响"}},
        ],
    },
    {
        "role": "assistant", "name": "主理人",
        "content": [{
            "type": "hint", "hint": '<team-message from="政策研究员">政策稳定</team-message>',
            "source": json.dumps({"label": "team", "sublabel": "政策研究员"}),
        }],
    },
]


class TestStore:
    def test_missing_file(self, env):
        assert ArchiveStore(env / "nope.json").load() == []

    def test_add_get(self, env):
        s = ArchiveStore(env / "a.json")
        s.add({"id": "x1", "name": "封档1"})
        assert s.get("x1")["name"] == "封档1"
        assert s.get("missing") is None

    def test_corrupted_degrades(self, env):
        (env / "a.json").write_text("[bad", encoding="utf-8")
        assert ArchiveStore(env / "a.json").load() == []


class TestTranscript:
    def test_text_and_tools(self):
        t = _messages_to_transcript(FAKE_MESSAGES)
        assert "山东考生" in t
        assert "[调用工具 TeamCreate" in t
        assert "[团队消息]" in t and "政策稳定" in t
        assert "主理人:" in t

    def test_empty(self):
        assert _messages_to_transcript([]) == ""
        assert _messages_to_transcript([{"role": "x", "content": []}]) == ""

    def test_truncation(self):
        msgs = [{
            "role": "user", "name": "u",
            "content": [{"type": "text", "text": "长" * 10000}],
        }]
        assert len(_messages_to_transcript(msgs)) <= 60000 + 20


class TestSummarize:
    def _patch_official(self, monkeypatch, messages, status=200):
        class FakeResp:
            def __init__(self):
                self.status_code = status
                self._json = {"messages": messages}

            def json(self):
                return self._json

        async def fake_call(method, path, user_id, json_body=None, params=None):
            return FakeResp()

        monkeypatch.setattr(ta, "_call_official", fake_call)

    def test_summarize_success(self, client, monkeypatch):
        self._patch_official(monkeypatch, FAKE_MESSAGES)

        async def fake_llm(prompt, system=None):
            assert "山东考生" in prompt  # 纪要进入了 LLM
            return {
                "summary": "完成志愿方案",
                "workflow_steps": ["建队", "分派", "整合"],
                "new_agents": [{
                    "name": "政策研究员", "description": "政策",
                    "system_prompt": "你是政策专家",
                }],
            }

        monkeypatch.setattr(ta, "llm_chat_json", fake_llm)
        r = client.post("/team-archive/summarize", json={
            "agent_id": "aid", "session_id": "sid",
        })
        d = r.json()
        assert d["summary"] == "完成志愿方案"
        assert d["workflow_steps"] == ["建队", "分派", "整合"]
        assert d["new_agents"][0]["name"] == "政策研究员"
        assert d["fallback"] is False

    def test_summarize_llm_failure_fallback(self, client, monkeypatch):
        self._patch_official(monkeypatch, FAKE_MESSAGES)

        async def bad_llm(prompt, system=None):
            raise RuntimeError("api down")

        monkeypatch.setattr(ta, "llm_chat_json", bad_llm)
        r = client.post("/team-archive/summarize", json={
            "agent_id": "aid", "session_id": "sid",
        })
        d = r.json()
        assert d["fallback"] is True
        assert "人工填写" in d["summary"]

    def test_summarize_session_unreadable(self, client, monkeypatch):
        self._patch_official(monkeypatch, [], status=404)
        r = client.post("/team-archive/summarize", json={
            "agent_id": "aid", "session_id": "sid",
        })
        assert r.status_code == 404

    def test_summarize_empty_session(self, client, monkeypatch):
        self._patch_official(monkeypatch, [])
        r = client.post("/team-archive/summarize", json={
            "agent_id": "aid", "session_id": "sid",
        })
        assert r.status_code == 400

    def test_summarize_requires_login(self, env):
        from fastapi import FastAPI as F
        app = F()
        app.include_router(team_archive_router)
        c = TestClient(app)  # 无 X-User-ID
        r = c.post("/team-archive/summarize", json={
            "agent_id": "a", "session_id": "s",
        })
        assert r.status_code == 401


class TestArchive:
    def test_create_and_get(self, client, env):
        r = client.post("/team-archive", json={
            "name": "高考志愿团队",
            "summary": "总结",
            "workflow_steps": ["步骤1", "步骤2"],
            "team_members": [{"id": "m1", "name": "研究员"}],
            "new_agents": [],
        })
        assert r.status_code == 200
        d = r.json()
        assert d["name"] == "高考志愿团队"
        assert d["team_members"] == [{"id": "m1", "name": "研究员"}]

        lst = client.get("/team-archive").json()
        assert len(lst) == 1
        detail = client.get(f"/team-archive/{d['id']}").json()
        assert detail["workflow_steps"] == ["步骤1", "步骤2"]

    def test_create_registers_new_agents(self, client, env, monkeypatch):
        """封档注册链路（真实契约）：新成员经 POST /agent/ 入库，
        响应顶层是 agent_id——封档记录必须捕获到该 id（曾因此丢失注册信息）。"""
        from tests.official_contract import post_agent_response

        registered_calls = []

        async def fake_call(method, path, user_id, json_body=None, params=None):
            registered_calls.append((method, path, json_body))
            class FakeResp:
                status_code = 200
                def json(self):
                    return post_agent_response("new-agent-1")
                def raise_for_status(self):
                    pass
            return FakeResp()

        monkeypatch.setattr(ta, "_call_official", fake_call)
        r = client.post("/team-archive", json={
            "name": "含新agent的封档",
            "summary": "s",
            "new_agents": [{
                "name": "政策研究员", "description": "d",
                "system_prompt": "你是专家",
            }],
        })
        d = r.json()
        assert d["new_agents"] == [{
            "id": "new-agent-1", "name": "政策研究员", "description": "d",
        }]
        # 注册调用：member 类型 + 系统提示词
        method, path, body = registered_calls[0]
        assert method == "POST" and path == "/agent/"
        assert body["agent_type"] == "member"
        assert body["system_prompt"] == "你是专家"
        assert body["name"] == "政策研究员"

    def test_create_empty_name_rejected(self, client):
        r = client.post("/team-archive", json={"name": "  ", "summary": "s"})
        assert r.status_code == 422

    def test_get_missing_404(self, client):
        assert client.get("/team-archive/nope").status_code == 404

    def test_registration_failure_not_fatal(self, client, env, monkeypatch):
        async def bad_call(method, path, user_id, json_body=None, params=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(ta, "_call_official", bad_call)
        r = client.post("/team-archive", json={
            "name": "N", "summary": "s",
            "new_agents": [{"name": "x", "description": "", "system_prompt": ""}],
        })
        assert r.status_code == 200  # 注册失败不影响封档本身
        assert r.json()["new_agents"] == []
