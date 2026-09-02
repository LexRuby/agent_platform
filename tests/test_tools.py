"""中台工具层测试：重试策略、HTTP 错误处理、to_chunk 序列化。

_http 客户端用 httpx.MockTransport 替换，不发真实请求。
"""

import asyncio
import json

import httpx
import pytest

from app.tools import base as tb
from app.tools.base import MidplatformTool
from app.tools.writing import WritingGenerate


class _Tool(MidplatformTool):
    """测试用具体工具（MidplatformTool 是抽象基类，endpoint 决定路径）。"""
    name = "test_tool"
    description = "测试工具"
    endpoint = "/api/test"


def make_tool_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://midplatform",
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    """重试等待压缩为 0，避免测试慢。"""

    class _FastAsyncio:
        @staticmethod
        async def sleep(seconds):
            return None

    monkeypatch.setattr(tb, "asyncio", _FastAsyncio())


class TestPost:
    async def test_success(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json={"ok": True, "data": 1})

        monkeypatch.setattr(tb, "_client", make_tool_client(handler))
        result = await _Tool().post({"q": "x"})
        assert result == {"ok": True, "data": 1}

    async def test_payload_passed_as_json(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={})

        monkeypatch.setattr(tb, "_client", make_tool_client(handler))
        await _Tool().post({"q": "志愿", "n": 5})
        assert seen["url"].endswith("/api/test")
        assert seen["body"] == {"q": "志愿", "n": 5}

    async def test_retry_then_success(self, monkeypatch):
        """第一次 500、第二次 200 → 重试后成功。"""
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json={"recovered": True})

        monkeypatch.setattr(tb, "_client", make_tool_client(handler))
        result = await _Tool().post({})
        assert result == {"recovered": True}
        assert calls["n"] == 2

    async def test_all_retries_exhausted(self, monkeypatch):
        """连续失败 → 总尝试次数 = RETRIES+1，最终 RuntimeError。"""
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(503, text="down")

        monkeypatch.setattr(tb, "_client", make_tool_client(handler))
        with pytest.raises(RuntimeError, match="Midplatform call failed"):
            await _Tool().post({})
        assert calls["n"] == tb.RETRIES + 1

    async def test_connect_error_retried(self, monkeypatch):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(tb, "_client", make_tool_client(handler))
        with pytest.raises(RuntimeError):
            await _Tool().post({})
        assert calls["n"] == tb.RETRIES + 1

    async def test_client_error_not_retried_immediately_raises(self, monkeypatch):
        """4xx 属于 HTTPStatusError → 走重试路径并最终失败。"""
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(404, text="not found")

        monkeypatch.setattr(tb, "_client", make_tool_client(handler))
        with pytest.raises(RuntimeError):
            await _Tool().post({})
        assert calls["n"] == tb.RETRIES + 1


class TestToChunk:
    def test_serializes_json(self):
        chunk = _Tool().to_chunk({"a": 1})
        text = chunk.content[0].text
        assert json.loads(text) == {"a": 1}

    def test_unicode_not_escaped(self):
        chunk = _Tool().to_chunk({"学校": "清华大学"})
        assert "清华大学" in chunk.content[0].text

    def test_truncated_to_8000(self):
        chunk = _Tool().to_chunk({"blob": "x" * 20000})
        assert len(chunk.content[0].text) <= 8000


class TestWritingTools:
    """写作工具的 schema 与端点绑定。"""

    def test_generate_endpoint(self):
        assert WritingGenerate().endpoint == "/writing/generate"

    def test_input_schema_required_fields(self):
        schema = WritingGenerate.input_schema
        assert set(schema["required"]) == {"title", "outline", "material"}

    def test_tool_metadata(self):
        t = WritingGenerate()
        assert t.name == "writing_generate" and t.description

    async def test_call_roundtrip(self, monkeypatch):
        def handler(request):
            body = json.loads(request.content)
            return httpx.Response(
                200, json={"report": f"《{body['title']}》{body['material']}"},
            )

        monkeypatch.setattr(tb, "_client", make_tool_client(handler))
        chunk = await WritingGenerate().call(
            title="志愿报告", outline="冲稳保", material="621分",
        )
        text = chunk.content[0].text
        assert "志愿报告" in text and "621分" in text
