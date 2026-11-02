import json
from dataclasses import dataclass

import fakeredis.aioredis
import pytest

from app import patent_observations as observations
from app.patent_search_tools import PATENT_AGENT_PROFILE_MARKER


@dataclass
class _Data:
    name: str = "专利小A"
    system_prompt: str = PATENT_AGENT_PROFILE_MARKER


@dataclass
class _Agent:
    data: _Data


@dataclass
class _Message:
    id: str
    role: str = "assistant"
    content: list | None = None
    created_at: str = "2026-11-02T10:00:00"

    def model_dump(self, mode="json"):
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content or [],
            "created_at": self.created_at,
        }


class _Storage:
    def __init__(self):
        self._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.messages = []

    async def get_agent(self, user_id, agent_id):
        return _Agent(_Data()) if (user_id, agent_id) == ("alice", "a1") else None

    async def list_messages(self, user_id, session_id, limit=100):
        return self.messages[:limit], len(self.messages) > limit


@pytest.mark.asyncio
async def test_archive_is_account_scoped_and_updates_streamed_message():
    storage = _Storage()
    await storage._client.set(
        "agentscope:user:alice:session:s1",
        json.dumps({"agent_id": "a1"}),
    )
    first = _Message("m1", content=[{"type": "text", "text": "初稿"}])
    final = _Message("m1", content=[{"type": "text", "text": "定稿"}])

    assert await observations.archive_message(storage, "alice", "s1", first)
    assert await observations.archive_message(storage, "alice", "s1", final)

    values = await storage._client.hvals(
        "agentforge:patent-observation:alice:a1",
    )
    assert len(values) == 1
    assert json.loads(values[0])["content"][0]["text"] == "定稿"
    assert await storage._client.smembers(
        "agentforge:patent-observation-sessions:alice:a1",
    ) == {"s1"}


@pytest.mark.asyncio
async def test_non_patent_or_unknown_session_is_not_archived():
    storage = _Storage()
    assert not await observations.archive_message(
        storage, "alice", "missing", _Message("m1"),
    )


def test_public_record_redacts_physical_workspace_path():
    physical = (
        "/data/alice/AgentScope/agentforge/data/workspaces/"
        "3e037862f67083f048cd1cd611adfdea/conversations/"
        "d97264527127e078c3605f71857029ad/result.md"
    )
    assert observations._public_value({"path": physical}) == {
        "path": "./result.md",
    }
