"""Context policies that preserve specialized-agent semantics."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from agentscope.agent import Agent, ContextConfig
from agentscope.event import AgentEvent
from agentscope.message import Msg

from app.patent_search_tools import (
    PATENT_ANALYSIS_PROFILE_MARKER,
    PATENT_EXTRACTION_PROFILE_MARKER,
)


def context_config_for_prompt(prompt: str, config: ContextConfig | None) -> ContextConfig:
    """Apply specialized defaults only when platform defaults are unchanged."""
    current = config or ContextConfig()
    if current.trigger_ratio != 0.8 or current.reserve_ratio != 0.1 or current.tool_result_limit != 50_000:
        return current
    if PATENT_EXTRACTION_PROFILE_MARKER in prompt:
        return current.model_copy(update={
            "trigger_ratio": 0.9,
            "reserve_ratio": 0.05,
            "tool_result_limit": 50_000,
            "max_image_num": 2,
        })
    if PATENT_ANALYSIS_PROFILE_MARKER in prompt:
        return current.model_copy(update={
            "trigger_ratio": 0.72,
            "reserve_ratio": 0.1,
            "tool_result_limit": 24_000,
            "max_image_num": 3,
        })
    return current


class ContextPolicyAgent(Agent):
    """Use profile defaults and isolate each extraction request."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        prompt = str(kwargs.get("system_prompt", ""))
        if len(args) > 1 and not prompt:
            prompt = str(args[1])
        self._agentforge_extraction = PATENT_EXTRACTION_PROFILE_MARKER in prompt
        kwargs["context_config"] = context_config_for_prompt(
            prompt,
            kwargs.get("context_config"),
        )
        super().__init__(*args, **kwargs)

    async def reply_stream(self, inputs=None, *args: Any, **kwargs: Any) -> AsyncGenerator[AgentEvent | Msg, None]:
        # Each extraction batch is a standalone task. Prior batches and old
        # summaries must not consume the current input window.
        if self._agentforge_extraction and isinstance(inputs, (Msg, list)):
            self.state.context = []
            self.state.summary = ""
            self.state.reply_context = type(self.state.reply_context)()
            self.state.middle_context = {}
        async for event in super().reply_stream(inputs, *args, **kwargs):
            yield event
