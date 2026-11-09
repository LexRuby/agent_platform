"""OpenTelemetry 可观测性：traces（Jaeger）+ metrics（Prometheus）。

三层接入，全部由环境变量开关（未配置时零开销短路）：

1. ``setup_observability``：进程级 TracerProvider / MeterProvider +
   OTLP 导出（traces → Jaeger :4318，metrics → Prometheus OTLP write）。
2. ``patch_runtime_observability``：复用技能沉淀 SOP 的 patch 模式，
   替换官方 ``_chat.Agent``——所有大A/小A（含 AgentCreate 动态成员与
   存量）聊天时自动挂 TracingMiddleware（Agent/LLM/工具 span）+
   LlmMetricsMiddleware（LLM 延迟与 token 用量）。
3. ``ObservabilityASGIMiddleware``：HTTP 入口 span + RED 指标
   （请求数/延迟/状态码，按路由模板聚合），覆盖会话 API 与 MCP 网关；
   解析 W3C traceparent 支持外部调用方串联。

与 usage_metering（Redis 按 user/agent/model 计量 + 查询 API）互补：
这里输出的是标准 OTLP 指标，可被 Prometheus 聚合告警。
"""

import logging
import time
from typing import Any, AsyncGenerator, Awaitable, Callable

from opentelemetry import metrics as otel_metrics
from opentelemetry import propagate, trace

_logger = logging.getLogger("agentforge.observability")

# ---------------------------------------------------------------------------
# 进程级初始化
# ---------------------------------------------------------------------------

_traces_enabled = False
_metrics_enabled = False
_meter: otel_metrics.Meter | None = None

# 指标仪表（setup 成功后创建；None = 未启用）
_http_requests: otel_metrics.Counter | None = None
_http_duration: otel_metrics.Histogram | None = None
_llm_duration: otel_metrics.Histogram | None = None
_llm_tokens: otel_metrics.Counter | None = None


def setup_observability(
    traces_endpoint: str,
    metrics_endpoint: str,
) -> bool:
    """初始化 OTLP traces/metrics 导出。任一 endpoint 非空即启用对应信号。

    幂等：重复调用不重复装配。返回是否至少启用了一个信号。
    """
    global _traces_enabled, _metrics_enabled, _meter
    global _http_requests, _http_duration, _llm_duration, _llm_tokens

    if _traces_enabled or _metrics_enabled:
        return True

    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({"service.name": "agentforge"})

    if traces_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_endpoint)),
        )
        trace.set_tracer_provider(provider)
        _traces_enabled = True

    if metrics_endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=metrics_endpoint),
            export_interval_millis=15000,
        )
        otel_metrics.set_meter_provider(
            MeterProvider(resource=resource, metric_readers=[reader]),
        )
        _meter = otel_metrics.get_meter("agentforge")
        _http_requests = _meter.create_counter(
            "agentforge_http_requests_total",
            unit="{request}",
            description="HTTP 请求数（method/route/status）",
        )
        _http_duration = _meter.create_histogram(
            "agentforge_http_duration_seconds",
            unit="s",
            description="HTTP 请求耗时分布",
        )
        _llm_duration = _meter.create_histogram(
            "agentforge_llm_duration_seconds",
            unit="s",
            description="LLM 调用耗时分布（含流式全程）",
        )
        _llm_tokens = _meter.create_counter(
            "agentforge_llm_tokens_total",
            unit="{token}",
            description="LLM token 用量（type=input/output/cache）",
        )
        _metrics_enabled = True

    if _traces_enabled or _metrics_enabled:
        _logger.info(
            "可观测性已启用 traces=%s metrics=%s",
            _traces_enabled,
            _metrics_enabled,
        )
        return True
    return False


def observability_enabled() -> bool:
    """traces 或 metrics 任一启用。"""
    return _traces_enabled or _metrics_enabled


def _snapshot() -> tuple:
    """当前启用状态与仪表快照（测试还原用）。"""
    return (
        _traces_enabled,
        _metrics_enabled,
        _http_requests,
        _http_duration,
        _llm_duration,
        _llm_tokens,
    )


def _restore(saved: tuple) -> None:
    global _traces_enabled, _metrics_enabled
    global _http_requests, _http_duration, _llm_duration, _llm_tokens
    (
        _traces_enabled,
        _metrics_enabled,
        _http_requests,
        _http_duration,
        _llm_duration,
        _llm_tokens,
    ) = saved


# ---------------------------------------------------------------------------
# AgentScope 中间件：LLM 延迟 + token 用量
# ---------------------------------------------------------------------------


def _usage_of(response: Any) -> tuple[int, int, int] | None:
    """从 ChatResponse（含流式末块）提取 (input, output, cache) tokens。"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    cache_tok = getattr(usage, "cache_input_tokens", 0) or 0
    if not (in_tok or out_tok or cache_tok):
        return None
    return in_tok, out_tok, cache_tok


def _model_label(input_kwargs: dict) -> str:
    """模型名（指标标签）。取不到时退回类名，避免空标签高基数。"""
    model = input_kwargs.get("current_model")
    name = getattr(model, "model", None)
    return str(name) if name else type(model).__name__


class LlmMetricsMiddleware:
    """AgentScope 中间件：记录每次 LLM 调用耗时与 token 用量。

    与 TracingMiddleware 同一挂载点（``_chat.Agent`` patch），因此
    大A/小A、流式/非流式全覆盖。未启用 metrics 时整体短路。
    """

    async def on_model_call(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        if _llm_duration is None and _llm_tokens is None:
            return await next_handler(**input_kwargs)

        model = _model_label(input_kwargs)
        start = time.perf_counter()
        result = await next_handler(**input_kwargs)

        def _record(response: Any) -> None:
            if _llm_duration is not None:
                _llm_duration.record(time.perf_counter() - start, {"model": model})
            usage = _usage_of(response)
            if usage and _llm_tokens is not None:
                in_tok, out_tok, cache_tok = usage
                if in_tok:
                    _llm_tokens.add(in_tok, {"model": model, "type": "input"})
                if out_tok:
                    _llm_tokens.add(out_tok, {"model": model, "type": "output"})
                if cache_tok:
                    _llm_tokens.add(cache_tok, {"model": model, "type": "cache"})

        if isinstance(result, AsyncGenerator):
            return self._trace_stream(result, _record)
        _record(result)
        return result

    @staticmethod
    async def _trace_stream(
        gen: AsyncGenerator,
        on_finish: Callable[[Any], None],
    ) -> AsyncGenerator:
        """流式响应：从最后一个数据块取 usage（末块才带完整统计）。"""
        last = None
        try:
            async for chunk in gen:
                last = chunk
                yield chunk
        finally:
            on_finish(last)


# ---------------------------------------------------------------------------
# Agent patch：所有大A/小A 挂 Tracing + LlmMetrics 中间件
# ---------------------------------------------------------------------------

_obs_agent_cls = None


def get_obs_agent_cls():
    """返回 patch 后的包装 Agent 类（未 patch 时 None；测试用）。"""
    return _obs_agent_cls


def patch_runtime_observability() -> bool:
    """替换官方 ``_chat.Agent`` 为自动挂观测中间件的包装类。

    与 patch_runtime_sop 同一模式：包装 ``__init__`` 向 ``middlewares``
    追加 TracingMiddleware + LlmMetricsMiddleware。两类 patch 任意顺序
    组合均成立（各自幂等、互不感知）。仅在可观测性启用时 patch，
    未启用零开销。返回是否 patch。
    """
    global _obs_agent_cls

    if not observability_enabled():
        return False

    from agentscope.agent import Agent as OfficialAgent
    from agentscope.app._service import _chat
    from agentscope.middleware import TracingMiddleware

    if getattr(_chat.Agent, "_agentforge_observability_injected", False):
        return True  # 已 patch（幂等）

    class ObservabilityAgent(_chat.Agent):
        """官方 Agent（可能已是 SOP 包装）再包一层：追加观测中间件。"""

        _agentforge_observability_injected = True

        def __init__(self, *, middlewares: list | None = None, **kwargs):
            merged = list(middlewares or [])
            merged.append(TracingMiddleware())
            if _llm_duration is not None or _llm_tokens is not None:
                merged.append(LlmMetricsMiddleware())
            super().__init__(middlewares=merged, **kwargs)

    _chat.Agent = ObservabilityAgent  # type: ignore[assignment]
    _obs_agent_cls = ObservabilityAgent
    _logger.info("运行时可观测中间件注入已启用（全 agent）")
    return True


# ---------------------------------------------------------------------------
# ASGI 中间件：HTTP 入口 span + RED 指标
# ---------------------------------------------------------------------------


def _route_label(scope: dict) -> str:
    """路由模板标签（低基数）：优先 Starlette 路由模板，静态资源归并。"""
    route = scope.get("route")
    path = scope.get("path", "")
    rp = getattr(route, "path", None)
    if rp and rp != "/":  # Mount("/") = 兜底静态/SPA，不算业务路由
        return rp
    if path.startswith("/assets/") or path in ("/", "/favicon.svg", "/favicon.ico"):
        return "static"
    return "unmatched"


class ObservabilityASGIMiddleware:
    """HTTP 入口可观测：server span + 请求数/耗时/状态码指标。

    放在中间件链最外层（AuthMiddleware 之外），登录页、会话 API、
    MCP 网关全覆盖。支持 W3C traceparent：外部调用方（如 MCP 客户端）
    的链路可串联。未启用时直接透传（零开销）。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not observability_enabled():
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers") or []
        }
        ctx = propagate.extract(headers)  # traceparent/baggage → 父上下文

        status_code = 500
        start = time.perf_counter()

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        span = None
        if _traces_enabled:
            tracer = trace.get_tracer("agentforge.http")
            span = tracer.start_span(
                name=f"HTTP {method} {scope.get('path', '')}",
                context=ctx,
                kind=trace.SpanKind.SERVER,
            )
            token = trace.use_span(span, end_on_exit=True)
            token.__enter__()
        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as e:
            if span is not None:
                span.record_exception(e)
                span.set_status(trace.StatusCode.ERROR, str(e))
            raise
        finally:
            route = _route_label(scope)
            if span is not None:
                # 路由确定后改用模板名，避免带 ID 的路径撑爆基数
                span.update_name(f"HTTP {method} {route}")
                span.set_attribute("http.request.method", method)
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status_code)
                token.__exit__(None, None, None)
            if _http_requests is not None:
                _http_requests.add(
                    1,
                    {"method": method, "route": route, "status": str(status_code)},
                )
            if _http_duration is not None:
                _http_duration.record(
                    time.perf_counter() - start,
                    {"method": method, "route": route},
                )
