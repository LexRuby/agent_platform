"""Paper-writing prompt profiles, tools, and installation behavior."""

from dataclasses import dataclass

import pytest

from app.paper_writing_tools import (
    PAPER_AGENT_PROFILE_MARKER,
    PAPER_TEMPLATE_NAMES,
    paper_writing_tools_for_agent,
)
from app.prompt_templates import list_prompt_templates


@dataclass
class _AgentData:
    system_prompt: str


@dataclass
class _Record:
    data: _AgentData


class _Storage:
    def __init__(self, prompt: str) -> None:
        self.record = _Record(_AgentData(prompt))

    async def get_agent(self, user_id: str, agent_id: str):
        return self.record


def test_seven_paper_templates_have_profile_and_fact_boundaries():
    templates = {item["name"]: item for item in list_prompt_templates()}
    assert set(PAPER_TEMPLATE_NAMES) <= templates.keys()
    for name in PAPER_TEMPLATE_NAMES:
        content = templates[name]["content"]
        assert PAPER_AGENT_PROFILE_MARKER in content
        assert "search_journals" in content
        assert "anysearch_search" in content
        assert "不得" in content
        assert "内部思维链" in content


@pytest.mark.asyncio
async def test_paper_agent_gets_bounded_journal_search():
    tools = await paper_writing_tools_for_agent(
        _Storage(PAPER_AGENT_PROFILE_MARKER),
        "admin",
        "agent-1",
    )
    assert [tool.name for tool in tools] == ["search_journals", "anysearch_search"]
    assert all(tool.is_read_only for tool in tools)


@pytest.mark.asyncio
async def test_regular_agent_does_not_get_paper_search():
    tools = await paper_writing_tools_for_agent(
        _Storage("普通助手"),
        "admin",
        "agent-2",
    )
    assert tools == []
