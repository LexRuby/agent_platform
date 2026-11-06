"""Profile-aware context policy regression tests."""

import pytest
from agentscope.agent import ContextConfig

from app.context_policy import ContextPolicyAgent, context_config_for_prompt
from app.patent_search_tools import (
    PATENT_ANALYSIS_PROFILE_MARKER,
    PATENT_EXTRACTION_PROFILE_MARKER,
)


def test_extraction_keeps_current_request_near_full_context_window():
    cfg = context_config_for_prompt(PATENT_EXTRACTION_PROFILE_MARKER, ContextConfig())
    assert cfg.trigger_ratio == 0.9
    assert cfg.reserve_ratio == 0.05
    assert cfg.tool_result_limit == 50_000
    assert cfg.max_image_num == 2


def test_analysis_has_room_for_history_compression():
    cfg = context_config_for_prompt(PATENT_ANALYSIS_PROFILE_MARKER, ContextConfig())
    assert cfg.trigger_ratio == 0.72
    assert cfg.reserve_ratio == 0.1
    assert cfg.tool_result_limit == 24_000


def test_explicit_context_tuning_is_preserved():
    custom = ContextConfig(trigger_ratio=0.6, reserve_ratio=0.08, tool_result_limit=8_000)
    assert context_config_for_prompt(PATENT_EXTRACTION_PROFILE_MARKER, custom) is custom


@pytest.mark.asyncio
async def test_extraction_resets_old_context_before_new_user_message(monkeypatch):
    agent = ContextPolicyAgent(
        name="extract",
        system_prompt=PATENT_EXTRACTION_PROFILE_MARKER,
        model=object(),
    )
    agent.state.context = [{"old": True}]
    agent.state.summary = "old summary"
    agent.state.middle_context = {"old": True}

    async def fake_reply_stream(self, inputs=None, *args, **kwargs):
        yield "ok"

    monkeypatch.setattr("agentscope.agent.Agent.reply_stream", fake_reply_stream)
    events = [event async for event in agent.reply_stream([])]

    assert events == ["ok"]
    assert agent.state.context == []
    assert agent.state.summary == ""
    assert agent.state.middle_context == {}
