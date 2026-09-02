"""OpenTelemetry tracing 接入 AgentScope Studio（http://localhost:3000）。

AgentScope 2.0 无 agentscope.init（1.x API）；TracingMiddleware 使用全局
TracerProvider，因此这里配置 OTLP HTTP exporter 指向 Studio 即可。
未设置 AGENTFORGE_STUDIO_URL 时不做任何事（零开销短路）。
"""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_configured = False


def setup_tracing(studio_url: str) -> bool:
    global _configured
    if _configured or not studio_url:
        return False
    provider = TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{studio_url.rstrip('/')}/v1/traces")
        )
    )
    trace.set_tracer_provider(provider)
    _configured = True
    return True
