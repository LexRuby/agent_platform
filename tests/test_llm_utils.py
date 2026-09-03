"""llm_utils 单元测试：_extract_json 解析 + ARK 响应契约（MockTransport，不触网）。

覆盖背景：llm_chat_json 被 leader_team（AI 推荐成员）与 team_archive
（归档总结）共用，此前只有 monkeypatch 整体替换的间接覆盖，解析逻辑
（围栏/前后杂文/平衡片段）与 ARK 响应结构解析从未被直接测试。
"""

import httpx
import pytest

from app import llm_utils as lu


class TestExtractJson:
    """_extract_json：LLM 回复中抽取 JSON 的全部形态。"""

    def test_plain_object(self):
        assert lu._extract_json('{"a": 1}') == {"a": 1}

    def test_plain_array(self):
        assert lu._extract_json('[1, 2]') == [1, 2]

    def test_fenced_json(self):
        text = '好的，这是结果：\n```json\n{"recommendations": []}\n```\n以上。'
        assert lu._extract_json(text) == {"recommendations": []}

    def test_fenced_without_lang_tag(self):
        assert lu._extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_prose_wrapped_object(self):
        assert lu._extract_json('结果是 {"a": {"b": 2}} 请查收') == {"a": {"b": 2}}

    def test_prose_wrapped_array(self):
        assert lu._extract_json('列表：[{"x": 1}] 完毕') == [{"x": 1}]

    def test_nested_braces_in_strings(self):
        # 字符串里的花括号不应破坏平衡扫描
        assert lu._extract_json('{"s": "a}b{c"}') == {"s": "a}b{c"}

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="无法从 LLM 回复中解析 JSON"):
            lu._extract_json("完全不是 JSON")


def _ark_response(content: str) -> httpx.Response:
    """构造 ARK chat completions 响应（OpenAI 兼容契约）。"""
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
    )


class TestLlmChat:
    """llm_chat：httpx MockTransport 模拟 ARK，验证请求与响应契约。"""

    
    async def test_success(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            import json as _json
            body = _json.loads(request.content)
            captured["body"] = body
            return _ark_response("你好")

        transport = httpx.MockTransport(handler)
        _client = lu.httpx

        class PatchedClient(_client.AsyncClient):
            def __init__(self, **kw):
                kw["transport"] = transport
                super().__init__(**kw)

        lu.httpx = type("m", (), {"AsyncClient": PatchedClient, "Response": _client.Response})
        try:
            out = await lu.llm_chat("问题", system="系统", api_key="k1")
        finally:
            lu.httpx = _client
        assert out == "你好"
        assert captured["url"].endswith("/chat/completions")
        assert captured["auth"] == "Bearer k1"
        assert captured["body"]["messages"][0] == {"role": "system", "content": "系统"}
        assert captured["body"]["messages"][1] == {"role": "user", "content": "问题"}

    
    async def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("ARK_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="未配置 ARK_API_KEY"):
            await lu.llm_chat("x")

    
    async def test_json_roundtrip(self):
        """llm_chat_json 全链路：ARK 响应 → 围栏文本 → dict。"""
        transport = httpx.MockTransport(
            lambda req: _ark_response('```json\n{"recommendations": [{"id": "m1"}]}\n```'),
        )
        orig_client = lu.httpx.AsyncClient

        class PatchedClient(orig_client):
            def __init__(self, **kw):
                kw["transport"] = transport
                super().__init__(**kw)

        lu.httpx.AsyncClient = PatchedClient
        try:
            result = await lu.llm_chat_json("推荐", api_key="k")
        finally:
            lu.httpx.AsyncClient = orig_client
        assert result == {"recommendations": [{"id": "m1"}]}


class TestTimeoutBudget:
    """超时预算回归锁：思考型模型对长分析任务实测 30~120 秒。"""

    def test_default_timeout_ge_180(self):
        # 曾为 60 秒，归档总结线上必 ReadTimeout 且错误信息为空
        assert lu.TIMEOUT >= 180.0, (
            f"TIMEOUT={lu.TIMEOUT}s 不足以覆盖思考型模型的实际耗时"
        )

    def test_timeout_env_override(self, monkeypatch):
        monkeypatch.setenv("AGENTFORGE_LLM_TIMEOUT", "300")
        import importlib
        old = lu.TIMEOUT
        try:
            importlib.reload(lu)
            assert lu.TIMEOUT == 300.0
        finally:
            monkeypatch.delenv("AGENTFORGE_LLM_TIMEOUT")
            importlib.reload(lu)
            assert lu.TIMEOUT == old
