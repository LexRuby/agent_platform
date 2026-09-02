"""Registry 注册中心测试：MCP CRUD、Agent 状态机、工作空间、JSON 持久化。"""

import json

import pytest

from app.registry import AGT_DRAFT, AGT_LOCKED, AGT_PUBLISHED, Registry


@pytest.fixture
def reg(tmp_path):
    return Registry(tmp_path / "registry.json")


def make_agent(reg, name="助手", tools=None):
    return reg.create_agent(
        name=name, description="d", system_prompt="sp", tools=tools or [],
    )


class TestMcpServers:
    def test_add_and_get(self, reg):
        srv = reg.add_mcp_server("m1", "http://x/mcp", "tok", "desc")
        assert srv["name"] == "m1" and srv["tools"] == []
        assert reg.get_mcp_server("m1")["url"] == "http://x/mcp"

    def test_list_hides_token(self, reg):
        reg.add_mcp_server("m1", "http://x/mcp", "secret-token")
        listed = reg.list_mcp_servers()
        assert "token" not in listed[0]
        assert "secret-token" not in json.dumps(listed)

    def test_get_returns_token_internally(self, reg):
        """内部读取（refresh 时）必须能拿到 token，只是 list 不外泄。"""
        reg.add_mcp_server("m1", "http://x/mcp", "secret-token")
        assert reg.get_mcp_server("m1")["token"] == "secret-token"

    def test_remove(self, reg):
        reg.add_mcp_server("m1", "http://x")
        reg.remove_mcp_server("m1")
        assert reg.get_mcp_server("m1") is None
        reg.remove_mcp_server("nonexistent")  # 幂等，不抛错

    def test_update_tools_only_for_existing(self, reg):
        reg.update_mcp_tools("ghost", [{"name": "t"}])  # 不存在则忽略
        assert reg.get_mcp_server("ghost") is None
        reg.add_mcp_server("m1", "http://x")
        reg.update_mcp_tools("m1", [{"name": "t"}])
        assert reg.get_mcp_server("m1")["tools"] == [{"name": "t"}]

    def test_readd_overrides(self, reg):
        reg.add_mcp_server("m1", "http://old")
        reg.add_mcp_server("m1", "http://new")
        assert reg.get_mcp_server("m1")["url"] == "http://new"


class TestAgentLifecycle:
    def test_create_defaults_to_draft(self, reg):
        a = make_agent(reg)
        assert a["status"] == AGT_DRAFT
        assert a["tools"] == []
        assert len(a["id"]) == 12

    def test_list_filter_by_status(self, reg):
        a1 = make_agent(reg, "one")
        a2 = make_agent(reg, "two")
        reg.set_agent_status(a1["id"], AGT_LOCKED)
        assert [a["name"] for a in reg.list_agents(AGT_DRAFT)] == ["two"]
        assert [a["name"] for a in reg.list_agents(AGT_LOCKED)] == ["one"]
        assert len(reg.list_agents()) == 2

    def test_get_missing_returns_none(self, reg):
        assert reg.get_agent("nope") is None

    def test_update_draft_fields(self, reg):
        a = make_agent(reg, tools=["builtin:t1"])
        updated = reg.update_agent(
            a["id"], {"name": "新名", "tools": ["builtin:t1", "mcp:m:t2"]},
        )
        assert updated["name"] == "新名"
        assert updated["tools"] == ["builtin:t1", "mcp:m:t2"]
        # 未出现在 patch 的字段保持不变
        assert updated["system_prompt"] == "sp"

    def test_update_locked_rejected(self, reg):
        a = make_agent(reg)
        reg.set_agent_status(a["id"], AGT_LOCKED)
        with pytest.raises(PermissionError):
            reg.update_agent(a["id"], {"name": "x"})

    def test_update_published_rejected(self, reg):
        a = make_agent(reg)
        reg.set_agent_status(a["id"], AGT_PUBLISHED)
        with pytest.raises(PermissionError):
            reg.update_agent(a["id"], {"name": "x"})

    def test_update_missing_returns_none(self, reg):
        assert reg.update_agent("nope", {"name": "x"}) is None

    def test_full_state_machine(self, reg):
        """draft → locked → published 的完整生命周期。"""
        a = make_agent(reg)
        aid = a["id"]
        reg.set_agent_status(aid, AGT_LOCKED)
        reg.set_agent_status(aid, AGT_PUBLISHED)
        assert reg.get_agent(aid)["status"] == AGT_PUBLISHED

    def test_set_status_missing_returns_none(self, reg):
        assert reg.set_agent_status("nope", AGT_LOCKED) is None


class TestWorkspace:
    def test_get_creates_empty(self, reg):
        a = make_agent(reg)
        ws = reg.get_workspace(a["id"])
        assert ws == {"messages": []}

    def test_append_and_order(self, reg):
        a = make_agent(reg)
        reg.append_message(a["id"], "user", "你好")
        reg.append_message(a["id"], "assistant", "你好，有什么可以帮你？")
        msgs = reg.get_workspace(a["id"])["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[0]["content"] == "你好"
        assert all("at" in m for m in msgs)

    def test_clear(self, reg):
        a = make_agent(reg)
        reg.append_message(a["id"], "user", "x")
        reg.clear_workspace(a["id"])
        assert reg.get_workspace(a["id"])["messages"] == []

    def test_workspaces_are_isolated(self, reg):
        a1, a2 = make_agent(reg, "one"), make_agent(reg, "two")
        reg.append_message(a1["id"], "user", "for-a1")
        assert reg.get_workspace(a2["id"])["messages"] == []


class TestPersistence:
    def test_reload_from_disk(self, tmp_path):
        path = tmp_path / "registry.json"
        reg = Registry(path)
        a = make_agent(reg)
        reg.add_mcp_server("m1", "http://x", "tok")
        reg.append_message(a["id"], "user", "hi")

        reg2 = Registry(path)
        assert reg2.get_agent(a["id"])["name"] == "助手"
        assert reg2.get_mcp_server("m1")["token"] == "tok"
        assert reg2.get_workspace(a["id"])["messages"][0]["content"] == "hi"

    def test_write_is_valid_json(self, tmp_path):
        path = tmp_path / "sub" / "registry.json"  # 父目录不存在
        reg = Registry(path)
        make_agent(reg)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["agents"]) == 1
