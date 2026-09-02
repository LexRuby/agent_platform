import asyncio

import httpx
from agentscope.message import TextBlock
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk

from app.settings import Settings

_client: httpx.AsyncClient | None = None
_settings: Settings | None = None

RETRIES = 2
TIMEOUT = 30.0


def init_http(settings: Settings) -> None:
    global _client, _settings
    _settings = settings
    headers = {}
    if settings.midplatform_token:
        headers["Authorization"] = f"Bearer {settings.midplatform_token}"
    _client = httpx.AsyncClient(
        base_url=settings.midplatform_base_url,
        timeout=TIMEOUT,
        headers=headers,
    )


async def close_http() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class MidplatformTool(ToolBase):
    is_concurrency_safe = True
    is_read_only = True
    endpoint: str = ""

    async def check_permissions(
        self,
        tool_input: dict,
        context: PermissionContext,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Intranet capability service call.",
        )

    async def post(self, payload: dict) -> dict:
        for attempt in range(RETRIES + 1):
            try:
                resp = await _client.post(self.endpoint, json=payload)
                resp.raise_for_status()
                return resp.json()
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPStatusError) as exc:
                if attempt == RETRIES:
                    raise RuntimeError(
                        f"Midplatform call failed after {RETRIES + 1} attempts "
                        f"({self.endpoint}): {exc}"
                    ) from exc
                await asyncio.sleep(0.5 * (2**attempt))

    def to_chunk(self, data: dict) -> ToolChunk:
        import json

        return ToolChunk(content=[TextBlock(text=json.dumps(data, ensure_ascii=False)[:8000])])
