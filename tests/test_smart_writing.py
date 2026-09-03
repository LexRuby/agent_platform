"""smart_writing MCP Server 测试：注册、路由、信封、错误透传、协议端点。

测试隔离原则：
- 注册包 → tmp_path 自建 mini 注册包（结构同 mcp_registry/smart_writing.json）
- HTTP 后端 → httpx.MockTransport（monkeypatch call_backend 的 client 注入）
- 真实注册包只做结构自检（文档注意事项 1：description 原样透传）

覆盖注册包文档的全部要求：
- 9 工具 name/description/inputSchema 原样注册
- x_backend 路由（mode/action/always）+ query_fields 过滤 + source=vr 信封
- 条件必填校验（filter_solutions/user_results）
- 错误透传（error_msg 原文给 Agent）
- 慢工具超时（extract_solutions/filter_solutions ≥300s）
"""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.smart_writing as sw


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """mini 注册包：真实包的结构，缩减 description 长度。"""
    pkg = {
        "package": "smart-writing-mcp",
        "server_name": "smart-writing",
        "tools": [
            {
                "name": "search_patents",
                "description": "专利文献检索（测试）",
                "inputSchema": {
                    "type": "object",
                    "properties": {"mode": {"type": "string"}, "query": {"type": "string"}, "source": {"type": "array"}, "size": {"type": "integer"}},
                    "required": ["mode", "query"],
                },
                "x_backend": [
                    {"when": "mode=natural", "endpoint": "http://backend-a/consult", "method": "patent_search", "query_fields": ["query", "size"]},
                    {"when": "mode=boolean", "endpoint": "http://backend-a/consult", "method": "high_patent_search", "query_fields": ["query", "source", "size"]},
                ],
            },
            {
                "name": "filter_solutions",
                "description": "方案筛选/聚合（测试）",
                "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}, "query": {"type": "string"}, "method_df": {"type": "array"}}, "required": ["action", "method_df"]},
                "x_backend": [
                    {"when": "action=filter", "endpoint": "http://backend-b/consult", "method": "solution2result", "query_fields": ["query", "method_df"]},
                    {"when": "action=cluster", "endpoint": "http://backend-b/consult", "method": "result_cluster", "query_fields": ["method_df"]},
                ],
            },
            {
                "name": "user_results",
                "description": "用户存档（测试）",
                "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}, "user_id": {"type": "string"}, "name": {"type": "string"}, "time": {"type": "string"}, "info": {"type": "string"}}, "required": ["action", "user_id"]},
                "x_backend": [
                    {"when": "action=save", "endpoint": "http://backend-b/consult", "method": "save_info", "query_fields": ["user_id", "time", "name", "info"]},
                    {"when": "action=list", "endpoint": "http://backend-b/consult", "method": "list_names", "query_fields": ["user_id"]},
                    {"when": "action=fetch", "endpoint": "http://backend-b/consult", "method": "get_detail", "query_fields": ["user_id", "name"]},
                ],
            },
        ],
    }
    p = tmp_path / "reg.json"
    p.write_text(json.dumps(pkg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("AGENTFORGE_SMART_WRITING_REGISTRY", str(p))
    return p


class TestLoadTools:
    def test_load_real_package(self):
        """真实注册包必须可加载：9 个工具、字段齐全（上线自检）。"""
        tools = sw.load_tools(Path(__file__).parent.parent / "mcp_registry" / "smart_writing.json")
        names = {t["name"] for t in tools}
        assert names == {
            "search_patents", "search_by_principle", "search_journals",
            "get_patent_details", "detect_word_ontology", "extract_solutions",
            "filter_solutions", "generate_with_prompt", "user_results",
        }
        # 路由总表（文档第 3 节）：13 method → 9 工具
        assert sum(len(t["x_backend"]) for t in tools) == 13
        # 地址规则：搜索类在 backend-a(140.210.4.206:30010)，其余在 116.204.102.229:30020
        for t in tools:
            for r in t["x_backend"]:
                if t["name"] in ("search_patents", "search_by_principle", "search_journals"):
                    assert ":30010" in r["endpoint"]
                else:
                    assert ":30020" in r["endpoint"]

    def test_description_passthrough_real_package(self):
        """注意事项 1：description 必须原样注册不压缩——负例防混选。"""
        raw = json.loads(
            (Path(__file__).parent.parent / "mcp_registry" / "smart_writing.json")
            .read_text(encoding="utf-8"),
        )
        raw_map = {t["name"]: t for t in raw["tools"]}
        for t in sw.load_tools(
            Path(__file__).parent.parent / "mcp_registry" / "smart_writing.json",
        ):
            assert t["description"] == raw_map[t["name"]]["description"]
            assert t["inputSchema"] == raw_map[t["name"]]["inputSchema"]

    def test_load_rejects_invalid(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"tools": [
            {"name": "x", "description": "d", "inputSchema": {"type": "object"}},
        ]}), encoding="utf-8")
        with pytest.raises(ValueError, match="x_backend"):
            sw.load_tools(bad)
        bad.write_text(json.dumps({"tools": [{"name": "x"}]}), encoding="utf-8")
        with pytest.raises(ValueError, match="缺字段 description"):
            sw.load_tools(bad)


class TestRouting:
    def test_when_condition_matches(self, registry):
        tools = {t["name"]: t for t in sw.load_tools()}
        r = sw.route_backend(tools["search_patents"], {"mode": "boolean"})
        assert r["method"] == "high_patent_search"
        r = sw.route_backend(tools["search_patents"], {"mode": "natural"})
        assert r["method"] == "patent_search"

    def test_when_always(self, registry):
        tools = {t["name"]: t for t in sw.load_tools()}
        r = sw.route_backend(tools["filter_solutions"], {"action": "cluster"})
        assert r["method"] == "result_cluster"

    def test_no_match_raises(self, registry):
        tools = {t["name"]: t for t in sw.load_tools()}
        with pytest.raises(RuntimeError, match="无匹配路由"):
            sw.route_backend(tools["user_results"], {"action": "bogus"})


class TestEnvelope:
    def test_query_fields_filter_and_source(self, registry):
        """只组装 query_fields 列出的参数，信封固定 source=vr。"""
        tools = {t["name"]: t for t in sw.load_tools()}
        routing = sw.route_backend(tools["search_patents"], {"mode": "natural"})
        env = sw.build_envelope(routing, {
            "query": "语音识别", "size": 5, "source": ["不该进 natural 的字段"],
        })
        assert env == {
            "utterance": {"query": {"query": "语音识别", "size": 5}},
            "method": "patent_search",
            "source": "vr",
        }

    def test_missing_fields_omitted(self, registry):
        tools = {t["name"]: t for t in sw.load_tools()}
        routing = sw.route_backend(tools["search_patents"], {"mode": "natural"})
        env = sw.build_envelope(routing, {"query": "q"})
        assert env["utterance"]["query"] == {"query": "q"}


class TestConditionalRequired:
    def test_filter_requires_query(self, registry):
        err = sw.validate_conditional_required(
            "filter_solutions", {"action": "filter", "method_df": []},
        )
        assert err is not None and "query" in err

    def test_cluster_no_query_needed(self, registry):
        assert sw.validate_conditional_required(
            "filter_solutions", {"action": "cluster", "method_df": []},
        ) is None

    def test_user_results_save_requires_all(self, registry):
        err = sw.validate_conditional_required(
            "user_results", {"action": "save", "user_id": "u1"},
        )
        assert "name" in err
        err2 = sw.validate_conditional_required(
            "user_results", {"action": "save", "user_id": "u1", "name": "n", "time": "t"},
        )
        assert "info" in err2

    def test_user_results_list_only_user_id(self, registry):
        assert sw.validate_conditional_required(
            "user_results", {"action": "list", "user_id": "u1"},
        ) is None

    def test_fetch_requires_name(self, registry):
        err = sw.validate_conditional_required(
            "user_results", {"action": "fetch", "user_id": "u1"},
        )
        assert "name" in err


class TestParseResponse:
    def test_data_and_cnt_dict(self):
        payload = sw.parse_response("search_patents", {
            "version": "1.0",
            "response": {"expire": 0, "data": [{"pubid": "CN1"}], "cnt_dict": {"appYear": {}}},
        })
        assert payload == {"data": [{"pubid": "CN1"}], "cnt_dict": {"appYear": {}}}

    def test_no_cnt_dict_omitted(self):
        payload = sw.parse_response("detect_word_ontology", {
            "response": {"data": {"f": []}},
        })
        assert payload == {"data": {"f": []}}

    def test_error_msg_passthrough(self):
        """注意事项 3：后端 error_msg 原文透传，不静默吞掉。"""
        with pytest.raises(RuntimeError, match="no skill returned any result"):
            sw.parse_response("high_journal_search", {
                "version": "1.0",
                "error_msg": "no skill returned any result",
                "skill": "dummy",
            })

    def test_missing_response_raises(self):
        with pytest.raises(RuntimeError, match="后端返回错误"):
            sw.parse_response("x", {"version": "1.0"})


class TestCallBackend:
    def _mock_client(self, handler):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        )

    @pytest.mark.anyio
    async def test_full_chain_routes_and_parses(self, registry):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "response": {"data": [{"ok": 1}]},
            })

        tools = {t["name"]: t for t in sw.load_tools()}
        async with self._mock_client(handler) as client:
            payload = await sw.call_backend(
                tools["search_patents"], {"mode": "boolean", "query": "title:x", "source": ["pubid"], "size": 2},
                client=client,
            )
        assert payload == {"data": [{"ok": 1}]}
        assert captured["url"] == "http://backend-a/consult"
        assert captured["body"] == {
            "utterance": {"query": {"query": "title:x", "source": ["pubid"], "size": 2}},
            "method": "high_patent_search",
            "source": "vr",
        }

    @pytest.mark.anyio
    async def test_conditional_required_short_circuits(self, registry):
        """条件必填失败时不发任何 HTTP 请求。"""
        tools = {t["name"]: t for t in sw.load_tools()}
        with pytest.raises(RuntimeError, match="query"):
            await sw.call_backend(
                tools["filter_solutions"], {"action": "filter", "method_df": []},
                client=self._mock_client(lambda r: httpx.Response(200, json={})),
            )

    @pytest.mark.anyio
    async def test_http_error_transparent(self, registry):
        tools = {t["name"]: t for t in sw.load_tools()}
        async with self._mock_client(
            lambda r: httpx.Response(502, text="bad gateway"),
        ) as client:
            with pytest.raises(RuntimeError, match="502"):
                await sw.call_backend(
                    tools["user_results"], {"action": "list", "user_id": "u1"},
                    client=client,
                )


class TestMcpEndpoint:
    """无状态 MCP JSON-RPC 协议（与官方 agentscope HttpMCPConfig 对接面）。"""

    @pytest.fixture
    def mcp(self, registry):
        return TestClient(sw.mcp_app)

    def _rpc(self, client, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            body["id"] = 1
        return client.post("/mcp", json=body)

    def test_initialize(self, mcp):
        r = self._rpc(mcp, "initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == 1
        assert body["result"]["protocolVersion"] == "2025-03-26"
        assert "tools" in body["result"]["capabilities"]
        assert body["result"]["serverInfo"]["name"] == "smart-writing"

    def test_notification_returns_202(self, mcp):
        r = self._rpc(mcp, "notifications/initialized", notify=True)
        assert r.status_code == 202

    def test_tools_list_passthrough(self, mcp):
        """tools/list 原样透传注册包的 name/description/inputSchema。"""
        r = self._rpc(mcp, "tools/list")
        tools = r.json()["result"]["tools"]
        by_name = {t["name"]: t for t in tools}
        assert set(by_name) == {"search_patents", "filter_solutions", "user_results"}
        assert by_name["search_patents"]["description"] == "专利文献检索（测试）"
        assert by_name["search_patents"]["inputSchema"]["required"] == ["mode", "query"]

    def test_tools_call_success(self, mcp, monkeypatch):
        async def fake_call(tool, arguments, *, client=None):
            assert tool["name"] == "search_patents"
            assert arguments["mode"] == "natural"
            return {"data": [{"pubid": "CN1"}]}

        monkeypatch.setattr(sw, "call_backend", fake_call)
        r = self._rpc(mcp, "tools/call", {"name": "search_patents", "arguments": {"mode": "natural", "query": "q"}})
        result = r.json()["result"]
        assert result.get("isError") is None
        assert json.loads(result["content"][0]["text"]) == {"data": [{"pubid": "CN1"}]}

    def test_tools_call_error_is_error_semantics(self, mcp, monkeypatch):
        """工具执行错误 → isError + 中文文案透传（Agent 可自行修正重试）。"""
        async def fake_call(tool, arguments, *, client=None):
            raise RuntimeError("action=filter 时必须提供 query（待解决技术问题）")

        monkeypatch.setattr(sw, "call_backend", fake_call)
        r = self._rpc(mcp, "tools/call", {"name": "filter_solutions", "arguments": {"action": "filter", "method_df": []}})
        result = r.json()["result"]
        assert result["isError"] is True
        assert "query" in result["content"][0]["text"]

    def test_unknown_tool(self, mcp):
        r = self._rpc(mcp, "tools/call", {"name": "nope", "arguments": {}})
        assert r.json()["error"]["code"] == -32602

    def test_unknown_method(self, mcp):
        r = self._rpc(mcp, "resources/list")
        assert r.json()["error"]["code"] == -32601

    def test_get_returns_405(self, mcp):
        """无状态 server：GET /mcp 返回 405，客户端走 POST。"""
        assert mcp.get("/mcp").status_code == 405

    def test_health_lists_tools(self, mcp):
        r = mcp.get("/health")
        assert r.status_code == 200
        assert set(r.json()["tools"]) == {"search_patents", "filter_solutions", "user_results"}


class TestTimeoutConfig:
    def test_slow_tools_config(self):
        """注意事项 4：批量 LLM 工具超时 ≥ 300s。"""
        assert sw._SLOW_TOOLS == {"extract_solutions", "filter_solutions"}
        assert sw._SLOW_TIMEOUT >= 300.0
        assert sw._DEFAULT_TIMEOUT < sw._SLOW_TIMEOUT

    def test_real_package_slow_tools_registered(self):
        """真实注册包里两个慢工具确实存在（超时配置不悬空）。"""
        tools = {t["name"] for t in sw.load_tools(
            Path(__file__).parent.parent / "mcp_registry" / "smart_writing.json",
        )}
        assert sw._SLOW_TOOLS <= tools
