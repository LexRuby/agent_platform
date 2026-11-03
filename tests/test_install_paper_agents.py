from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app.storage._model import AgentData, AgentRecord

from scripts import install_paper_agents as installer


class _FakeRedisStorage:
    records: dict[str, list[AgentRecord]] = {}

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def list_agents(self, user_id):
        return list(self.records.get(user_id, []))

    async def upsert_agent(self, user_id, record):
        records = self.records.setdefault(user_id, [])
        self.records[user_id] = [item for item in records if item.id != record.id]
        self.records[user_id].append(record)
        return record.id


def _record(user: str, name: str, prompt: str) -> AgentRecord:
    return AgentRecord(
        user_id=user,
        data=AgentData(
            name=name,
            system_prompt=prompt,
            context_config=ContextConfig(),
            react_config=ReActConfig(),
        ),
    )


async def test_install_is_idempotent_and_refreshes_prompt(tmp_path, monkeypatch):
    users_dir = tmp_path / "users"
    users_dir.mkdir()
    (users_dir / "alice.txt").write_text("password", encoding="utf-8")
    _FakeRedisStorage.records = {
        "alice": [_record("alice", "论文研究现状智能体", "旧提示词")],
    }
    monkeypatch.setattr(installer, "USERS_DIR", users_dir)
    monkeypatch.setattr(installer, "RedisStorage", _FakeRedisStorage)
    monkeypatch.setattr(
        installer,
        "_paper_templates",
        lambda: {
            "论文研究现状智能体": "新提示词",
            "论文结论智能体": "结论提示词",
        },
    )

    assert await installer.install(["alice"], "localhost", 6379, False) == 0
    first = _FakeRedisStorage.records["alice"]
    assert len(first) == 2
    original_id = next(
        item.id for item in first if item.data.name == "论文研究现状智能体"
    )
    assert next(
        item.data.system_prompt
        for item in first
        if item.data.name == "论文研究现状智能体"
    ) == "新提示词"

    assert await installer.install(["alice"], "localhost", 6379, False) == 0
    second = _FakeRedisStorage.records["alice"]
    assert len(second) == 2
    assert next(
        item.id for item in second if item.data.name == "论文研究现状智能体"
    ) == original_id


async def test_missing_account_is_skipped(tmp_path, monkeypatch):
    users_dir = tmp_path / "users"
    users_dir.mkdir()
    _FakeRedisStorage.records = {}
    monkeypatch.setattr(installer, "USERS_DIR", users_dir)
    monkeypatch.setattr(installer, "RedisStorage", _FakeRedisStorage)
    monkeypatch.setattr(installer, "_paper_templates", lambda: {"论文结论智能体": "提示词"})

    assert await installer.install(["ghost"], "localhost", 6379, False) == 1
    assert _FakeRedisStorage.records == {}
