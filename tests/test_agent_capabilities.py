from dataclasses import dataclass

import pytest

from app import agent_capabilities as capabilities
from app.patent_search_tools import PATENT_AGENT_PROFILE_MARKER


@dataclass
class _Data:
    name: str
    system_prompt: str


@dataclass
class _Agent:
    data: _Data


class _Storage:
    def __init__(self, prompt: str):
        self.agent = _Agent(_Data("测试小A", prompt))

    async def get_agent(self, user_id, agent_id):
        return self.agent if user_id == "alice" and agent_id == "a1" else None


@pytest.mark.asyncio
async def test_patent_agent_capabilities_are_visible():
    capabilities.init_agent_capabilities(
        _Storage(PATENT_AGENT_PROFILE_MARKER),
    )
    response = await capabilities.get_agent_capabilities("a1", "alice")
    body = response.model_dump()
    names = {
        tool["name"]
        for group in body["groups"]
        for tool in group["tools"]
    }
    assert {"Read", "search_patents", "get_patent_details", "check_claim_support"} <= names
    assert body["web_search_enabled"] is False
    assert any("没有通用互联网搜索" in note for note in body["notes"])


@pytest.mark.asyncio
async def test_capabilities_do_not_cross_accounts():
    capabilities.init_agent_capabilities(_Storage(PATENT_AGENT_PROFILE_MARKER))
    with pytest.raises(Exception) as exc:
        await capabilities.get_agent_capabilities("a1", "bob")
    assert getattr(exc.value, "status_code", None) == 404
