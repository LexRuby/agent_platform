"""可观测性（app.observability）测试。

覆盖：setup 开关/幂等、usage/route 纯函数、LLM 中间件（流式/非流式）、
ASGI 入口 span+指标、Agent patch 幂等与 SOP 组合。

全局 provider 说明：OTel 的 set_tracer_provider/set_meter_provider 每进程
仅一次生效——span 采集用 module 级 fixture 一次装配；指标断言全部走本地
MeterProvider + InMemoryMetricReader（不依赖全局）。
"""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from app import observability as obs  # noqa: E402


@pytest.fixture(scope="module")
def span_exporter():
    """进程内 span 采集（全局 tracer provider 仅首次设置生效）。"""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture()
def metric_env(span_exporter):
    """在本地 reader 上装配 observability 模块仪表（测后还原）。"""
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    saved = obs._snapshot()
    obs._traces_enabled = True
    obs._metrics_enabled = True
    obs._http_requests = meter.create_counter("agentforge_http_requests_total")
    obs._http_duration = meter.create_histogram("agentforge_http_duration_seconds")
    obs._llm_duration = meter.create_histogram("agentforge_llm_duration_seconds")
    obs._llm_tokens = meter.create_counter("agentforge_llm_tokens_total")
    yield reader
    obs._restore(saved)
    reader.shutdown()


def _metric_points(reader, name):
    """从 InMemory reader 提取 (attributes, value) 列表。"""
    points = []
    data = reader.get_metrics_data()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    for dp in metric.data.data_points:
                        key = tuple(
                            v for pair in sorted(dp.attributes.items()) for v in pair
                        )
                        # Counter 数据点有 value；Histogram 只有 sum/count
                        points.append((key, getattr(dp, "value", None) or dp.sum))
    return points


class _Usage:
    def __init__(self, i=0, o=0, c=0):
        self.input_tokens, self.output_tokens, self.cache_input_tokens = i, o, c


class _Response:
    def __init__(self, usage=None):
        self.usage = usage


class _Model:
    model = "test-model"


# ---------------------------------------------------------------------------
# setup 开关与幂等
# ---------------------------------------------------------------------------


def test_setup_empty_endpoints_disabled(monkeypatch):
    """两项 endpoint 均空 → 不启用任何信号。"""
    monkeypatch.setattr(obs, "_traces_enabled", False)
    monkeypatch.setattr(obs, "_metrics_enabled", False)
    assert obs.setup_observability("", "") is False
    assert obs.observability_enabled() is False


def test_setup_metrics_only_wiring_and_idempotent(monkeypatch):
    """仅 metrics endpoint → 启用并创建仪表；重复调用幂等。"""
    monkeypatch.setattr(obs, "_traces_enabled", False)
    monkeypatch.setattr(obs, "_metrics_enabled", False)
    for f in ("_http_requests", "_http_duration", "_llm_duration", "_llm_tokens"):
        monkeypatch.setattr(obs, f, None)
    # 指向死端口（127.0.0.1:1）：后台导出立即失败但不影响本测试
    assert obs.setup_observability("", "http://127.0.0.1:1/v1/metrics") is True
    assert obs._metrics_enabled is True
    assert obs._llm_tokens is not None and obs._http_requests is not None
    assert obs.setup_observability("", "http://127.0.0.1:2/x") is True  # 幂等短路


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_usage_of_extraction():
    """ChatResponse usage 提取与空值兜底。"""
    assert obs._usage_of(_Response(_Usage(10, 5, 2))) == (10, 5, 2)
    assert obs._usage_of(_Response(None)) is None
    assert obs._usage_of(_Response(_Usage())) is None  # 全 0 视为无用量
    assert obs._usage_of(object()) is None


def test_route_label_templates():
    """路由模板优先；静态资源归并；无路由时 unmatched。"""
    scope = {"route": SimpleNamespace(path="/chat/{agent}/{session}"), "path": "/x"}
    assert obs._route_label(scope) == "/chat/{agent}/{session}"
    assert obs._route_label({"path": "/assets/index-1.js"}) == "static"
    assert obs._route_label({"path": "/"}) == "static"
    assert obs._route_label({"path": "/api/xyz"}) == "unmatched"
    # Mount("/") 是 SPA 兜底，不算业务路由
    assert obs._route_label({"route": SimpleNamespace(path="/"), "path": "/a"}) == "unmatched"


# ---------------------------------------------------------------------------
# LLM 中间件
# ---------------------------------------------------------------------------


def test_llm_middleware_non_stream(metric_env):
    """非流式：延迟 + 三类 token 全记录。"""
    mw = obs.LlmMetricsMiddleware()

    async def run():
        async def handler(**kw):
            return _Response(_Usage(10, 5, 2))

        return await mw.on_model_call(None, {"current_model": _Model()}, handler)

    result = asyncio.run(run())
    assert isinstance(result, _Response)

    tokens = dict(_metric_points(metric_env, "agentforge_llm_tokens_total"))
    assert tokens == {
        ("model", "test-model", "type", "input"): 10,
        ("model", "test-model", "type", "output"): 5,
        ("model", "test-model", "type", "cache"): 2,
    }
    assert _metric_points(metric_env, "agentforge_llm_duration_seconds")


def test_llm_middleware_stream(metric_env):
    """流式：末块 usage 才统计，中间块不重复计。"""
    mw = obs.LlmMetricsMiddleware()
    chunks = [_Response(_Usage()), _Response(_Usage(7, 3))]

    async def run():
        async def handler(**kw):
            async def gen():
                for c in chunks:
                    yield c

            return gen()

        agen = await mw.on_model_call(None, {"current_model": _Model()}, handler)
        return [c async for c in agen]

    got = asyncio.run(run())
    assert len(got) == 2

    tokens = dict(_metric_points(metric_env, "agentforge_llm_tokens_total"))
    assert tokens == {
        ("model", "test-model", "type", "input"): 7,
        ("model", "test-model", "type", "output"): 3,
    }


def test_llm_middleware_disabled_passthrough(monkeypatch):
    """仪表未创建时零开销透传。"""
    for f in ("_llm_duration", "_llm_tokens"):
        monkeypatch.setattr(obs, f, None)
    mw = obs.LlmMetricsMiddleware()
    sentinel = _Response(_Usage(1, 1))

    async def run():
        async def handler(**kw):
            return sentinel

        return await mw.on_model_call(None, {"current_model": _Model()}, handler)

    assert asyncio.run(run()) is sentinel


# ---------------------------------------------------------------------------
# ASGI 入口中间件
# ---------------------------------------------------------------------------


async def _drive(app, *, method="GET", path="/chat/a/b"):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(
        {"type": "http", "method": method, "path": path, "headers": []},
        receive,
        send,
    )
    return messages


def test_asgi_middleware_span_and_red(metric_env, span_exporter):
    """入口 span（路由模板命名）+ RED 指标按模板聚合。"""
    span_exporter.clear()

    async def downstream(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/chat/{agent}/{session}")
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"{}"})

    asyncio.run(_drive(obs.ObservabilityASGIMiddleware(downstream)))

    spans = [s for s in span_exporter.get_finished_spans() if s.name.startswith("HTTP")]
    assert spans, "应产生入口 span"
    assert spans[-1].name == "HTTP GET /chat/{agent}/{session}"
    attrs = spans[-1].attributes
    assert attrs["http.route"] == "/chat/{agent}/{session}"
    assert attrs["http.response.status_code"] == 200

    points = dict(_metric_points(metric_env, "agentforge_http_requests_total"))
    assert points == {
        ("method", "GET", "route", "/chat/{agent}/{session}", "status", "200"): 1,
    }
    assert _metric_points(metric_env, "agentforge_http_duration_seconds")


def test_asgi_middleware_static_normalization(metric_env, span_exporter):
    """静态资源归并为 static 标签（低基数）。"""
    span_exporter.clear()

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b""})

    asyncio.run(_drive(obs.ObservabilityASGIMiddleware(downstream), path="/assets/index-1.js"))
    points = dict(_metric_points(metric_env, "agentforge_http_requests_total"))
    assert ("method", "GET", "route", "static", "status", "200") in points


def test_asgi_middleware_disabled_passthrough(monkeypatch):
    """未启用时直接透传（无 span/指标）。"""
    saved = obs._snapshot()
    monkeypatch.setattr(obs, "_traces_enabled", False)
    monkeypatch.setattr(obs, "_metrics_enabled", False)

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok"})

    messages = asyncio.run(_drive(obs.ObservabilityASGIMiddleware(downstream)))
    assert any(m["type"] == "http.response.body" for m in messages)


def test_asgi_middleware_error_span(metric_env, span_exporter):
    """下游异常：span 记录异常且指标记 500，异常向上抛。"""
    span_exporter.clear()

    async def downstream(scope, receive, send):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(_drive(obs.ObservabilityASGIMiddleware(downstream)))

    spans = [s for s in span_exporter.get_finished_spans() if s.name.startswith("HTTP")]
    assert spans and spans[-1].status.status_code.name == "ERROR"
    points = dict(_metric_points(metric_env, "agentforge_http_requests_total"))
    assert ("method", "GET", "route", "unmatched", "status", "500") in points


# ---------------------------------------------------------------------------
# Agent patch
# ---------------------------------------------------------------------------


def test_patch_agent_composes_and_idempotent(monkeypatch, span_exporter):
    """patch 后 _chat.Agent 带标记、幂等，且与 SOP 包装组合无冲突。"""
    from agentscope.app._service import _chat
    from agentscope.middleware import TracingMiddleware

    monkeypatch.setattr(obs, "_traces_enabled", True)
    monkeypatch.setattr(obs, "_metrics_enabled", True)
    original = _chat.Agent

    assert obs.patch_runtime_observability() is True
    try:
        assert getattr(_chat.Agent, "_agentforge_observability_injected", False)
        wrapped = _chat.Agent
        assert obs.patch_runtime_observability() is True  # 幂等
        assert _chat.Agent is wrapped

        # 包装类追加观测中间件（猴补父类构造拦截）
        cls = obs.get_obs_agent_cls()
        received = {}
        base = cls.__bases__[0]

        def fake_init(self, *, middlewares=None, **kw):
            received["middlewares"] = list(middlewares or [])

        monkeypatch.setattr(base, "__init__", fake_init)
        cls(name="x", middlewares=[])
        kinds = [type(m) for m in received["middlewares"]]
        assert TracingMiddleware in kinds

        # SOP 标记仍可叠加（两层包装正交）
        from app import skill_solidify

        skill_solidify.patch_runtime_sop()
        assert getattr(_chat.Agent, "_agentforge_sop_injected", False)
        assert getattr(_chat.Agent, "_agentforge_observability_injected", False)
    finally:
        monkeypatch.setattr(_chat, "Agent", original)
        monkeypatch.setattr(obs, "_obs_agent_cls", None)


def test_patch_agent_disabled_noop(monkeypatch):
    """未启用时不 patch（零开销）。"""
    from agentscope.app._service import _chat

    monkeypatch.setattr(obs, "_traces_enabled", False)
    monkeypatch.setattr(obs, "_metrics_enabled", False)
    original = _chat.Agent
    assert obs.patch_runtime_observability() is False
    assert _chat.Agent is original
