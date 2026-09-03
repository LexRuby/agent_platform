"""官方 API 契约快照测试：锁死 tests/official_contract.py 的结构。

契约模块是全部测试 mock 的单一事实源（见模块 docstring 的事故背景）。
本文件用两组断言保证它自身不漂移：

1. **快照**：顶层键列表与录制时完全一致——官方升级改动响应结构时，
   这里最先红，强制重新录制契约并同步修复中间件实现；
2. **工厂一致性**：所有工厂函数的输出必须携带快照键（防止工厂函数
   被改错，导致下游 mock 再次失真）。
"""

from tests import official_contract as oc


class TestContractSnapshot:
    """录制的真实响应结构快照（AgentScope 2.0.7.post1，2026-09-03）。"""

    def test_post_agent_response_keys(self):
        assert oc.POST_AGENT_RESPONSE_KEYS == ["agent_id"]

    def test_list_agent_response_keys(self):
        assert oc.LIST_AGENT_RESPONSE_KEYS == ["agents", "total"]

    def test_list_agent_item_keys(self):
        assert oc.LIST_AGENT_ITEM_KEYS == [
            "id", "user_id", "updated_at", "created_at", "source", "data",
            "editable",
        ]

    def test_session_messages_response_keys(self):
        assert oc.SESSION_MESSAGES_RESPONSE_KEYS == [
            "messages", "is_running", "has_more",
        ]

    def test_schema_v2_response_keys(self):
        assert oc.SCHEMA_V2_RESPONSE_KEYS == ["schema"]


class TestContractFactories:
    """工厂函数输出必须符合快照键。"""

    def test_post_agent_response(self):
        r = oc.post_agent_response("a1")
        assert list(r.keys()) == oc.POST_AGENT_RESPONSE_KEYS
        assert r["agent_id"] == "a1"

    def test_agent_item(self):
        a = oc.agent_item("a1", {"name": "x"})
        for k in oc.LIST_AGENT_ITEM_KEYS:
            assert k in a, f"列表项缺少契约键 {k}"
        assert a["data"]["name"] == "x"

    def test_list_agent_response(self):
        r = oc.list_agent_response([oc.agent_item("a1", {"name": "x"})])
        assert list(r.keys()) == oc.LIST_AGENT_RESPONSE_KEYS
        assert r["total"] == 1

    def test_session_messages_response(self):
        r = oc.session_messages_response([])
        assert list(r.keys()) == oc.SESSION_MESSAGES_RESPONSE_KEYS
        assert r["messages"] == [] and r["is_running"] is False

    def test_message_item(self):
        m = oc.message_item("m1", "assistant", "主理人", [
            {"type": "text", "text": "hi"},
        ])
        assert m["id"] == "m1"
        assert m["content"][0]["text"] == "hi"

    def test_schema_v2_response(self):
        r = oc.schema_v2_response({"name": {"type": "string"}})
        assert list(r.keys()) == oc.SCHEMA_V2_RESPONSE_KEYS
        assert "name" in r["schema"]["properties"]
