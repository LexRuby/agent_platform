"""智能体共享（账号 → 智能体可见性）测试——2026-09-07 共享 v1。

背景：账号隔离已天然存在（数据按 user_id 键控），用户要的是
**可见性控制**——"我能选择我发布之后，这些大A/小A 是什么账号
能看到的"。

实现（app/agent_share.py）：
- ``RedisAgentSharePolicy``：官方 ``ResourceAccessPolicyBase`` 的
  Redis 实现——授予的 agent 以只读 ResourceRef 暴露给官方
  ResourceAccessService（列表合并/403 编辑保护/会话聊天
  resolve_agent 全链路官方自带）
- 管理端点：GET /agent-share/mine、PUT /agent-share/{id}（发布/
  改可见性）、DELETE /agent-share/{id}（取消发布）

测试不依赖真实 Redis：
- Redis → fakeredis（FakeAsyncRedis）
- storage → FakeStorage（get_agent / list_agents 官方接口子集）
- 官方 ResourceAccessService 用**真类**（验证策略与官方链路的
  真实集成：列表合并 editable=false、resolve_for_edit 403、
  resolve_agent 放行）
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agentforge"))

from agentscope.app._service._access import ResourceAccessService  # noqa: E402
from agentscope.app.access import (  # noqa: E402
    ResourceKind,
    ResourcePermission,
)
from agentscope.app.storage import AgentRecord  # noqa: E402
from agentscope.app.storage._model._agent import (  # noqa: E402
    AgentData,
    ContextConfig,
    ReActConfig,
)

from app.agent_share import (  # noqa: E402
    RedisAgentSharePolicy,
    agent_share_router,
)


# ---------------------------------------------------------------- Fake 模型

@dataclass
class FakeStorage:
    """官方 RedisStorage 的接口子集（get_agent/list_agents/_client）。"""

    agents: dict[tuple[str, str], AgentRecord] = field(default_factory=dict)
    _client: fakeredis.FakeAsyncRedis = field(
        default_factory=lambda: fakeredis.FakeAsyncRedis(decode_responses=True)
    )

    async def get_agent(self, user_id: str, agent_id: str) -> AgentRecord | None:
        return self.agents.get((user_id, agent_id))

    async def list_agents(self, user_id: str) -> list[AgentRecord]:
        return [a for (u, _), a in self.agents.items() if u == user_id]


def _make_agent(owner: str, agent_id: str, name: str, source: str = "user") -> AgentRecord:
    return AgentRecord(
        id=agent_id,
        user_id=owner,
        source=source,
        data=AgentData(
            id=agent_id,
            name=name,
            context_config=ContextConfig(),
            react_config=ReActConfig(),
        ),
        created_at=datetime(2026, 9, 7, 12, 0, 0),
        updated_at=datetime(2026, 9, 7, 12, 0, 0),
    )


@pytest.fixture
def storage() -> FakeStorage:
    s = FakeStorage()
    # alice 的两个智能体 + bob 的一个
    s.agents[("alice", "a-leader")] = _make_agent("alice", "a-leader", "主理人（大A）")
    s.agents[("alice", "a-member")] = _make_agent("alice", "a-member", "政策研究员")
    s.agents[("alice", "a-team-worker")] = _make_agent(
        "alice", "a-team-worker", "团队成员", source="team"
    )
    s.agents[("bob", "b-agent")] = _make_agent("bob", "b-agent", "Bob 的智能体")
    return s


@pytest.fixture
def policy(storage) -> RedisAgentSharePolicy:
    return RedisAgentSharePolicy(storage)


@pytest.fixture
def api_app(storage, policy):
    """装载 agent_share_router 的最小 app（storage/policy 挂 state）。"""
    app = FastAPI()
    app.state.storage = storage
    app.state.resource_access_policy = policy
    app.include_router(agent_share_router)
    return app


def _client_of(app) -> TestClient:
    return TestClient(app)


async def _publish_users(
    client, owner: str, agent_id: str, users: list[str]
) -> None:
    """直写 Redis 模拟一次 users 模式发布（绕过端点，纯数据准备）。"""
    rec = {"owner": owner, "mode": "users", "users": users,
           "agent_name": "x", "shared_at": "2026-09-07T00:00:00"}
    await client.set(f"agentforge:share:agent:{agent_id}", json.dumps(rec))
    for u in users:
        await client.sadd(f"agentforge:share:to:{u}", agent_id)
    await client.sadd(f"agentforge:share:owner:{owner}", agent_id)


# ---------------------------------------------------------------- 策略层

class TestPolicy:
    """RedisAgentSharePolicy：授权解析（官方链路的供数方）。"""

    def test_no_shares_returns_empty(self, storage, policy):
        refs = asyncio.run(policy.list_accessible("bob", ResourceKind.AGENT, storage))
        assert refs == []

    def test_user_grant_yields_read_ref(self, storage, policy):
        asyncio.run(_publish_users(storage._client, "alice", "a-leader", ["bob"]))
        refs = asyncio.run(policy.list_accessible("bob", ResourceKind.AGENT, storage))
        assert len(refs) == 1
        r = refs[0]
        assert r.kind is ResourceKind.AGENT
        assert r.owner_id == "alice"
        assert r.resource_id == "a-leader"
        assert r.permission is ResourcePermission.READ, "共享必须只读（v1）"

    def test_ungranted_user_sees_nothing(self, storage, policy):
        asyncio.run(_publish_users(storage._client, "alice", "a-leader", ["bob"]))
        refs = asyncio.run(policy.list_accessible("carol", ResourceKind.AGENT, storage))
        assert refs == [], "未被授权的账号一个都看不到"

    def test_public_grant_visible_to_all(self, storage, policy):
        c = storage._client
        asyncio.run(
            c.set(
                "agentforge:share:agent:a-member",
                json.dumps({"owner": "alice", "mode": "public", "users": [],
                            "agent_name": "政策研究员", "shared_at": "t"}),
            )
        )
        asyncio.run(c.sadd("agentforge:share:public", "a-member"))
        for viewer in ("bob", "carol", "dave"):
            refs = asyncio.run(
                policy.list_accessible(viewer, ResourceKind.AGENT, storage)
            )
            assert [r.resource_id for r in refs] == ["a-member"]

    def test_owner_never_sees_own_via_policy(self, storage, policy):
        """自己的智能体走官方自有路径，策略不得重复返回。"""
        asyncio.run(_publish_users(storage._client, "alice", "a-leader", ["alice"]))
        refs = asyncio.run(policy.list_accessible("alice", ResourceKind.AGENT, storage))
        assert refs == []

    def test_stale_set_member_skipped(self, storage, policy):
        """Set 残留但记录已删 → 跳过（不炸、不出现幽灵授权）。"""
        c = storage._client
        asyncio.run(c.sadd("agentforge:share:to:bob", "ghost-agent"))
        refs = asyncio.run(policy.list_accessible("bob", ResourceKind.AGENT, storage))
        assert refs == []

    def test_credential_kind_not_supported(self, storage, policy):
        """v1 只共享智能体：凭据/知识库的 kind 一律空。"""
        from agentscope.app.access import ResourceKind as K

        assert asyncio.run(
            policy.list_accessible("bob", K.CREDENTIAL, storage)
        ) == []
        assert asyncio.run(
            policy.list_accessible("bob", K.KNOWLEDGE_BASE, storage)
        ) == []


# ---------------------------------------------------------------- 官方链路集成

class TestOfficialAccessIntegration:
    """策略 × 官方 ResourceAccessService：真实集成行为。"""

    def _access(self, storage, policy) -> ResourceAccessService:
        return ResourceAccessService(storage, policy)

    def test_list_merges_shared_with_editable_false(self, storage, policy):
        """bob 的列表 = 自己的 + alice 共享的（editable=False 只读徽标）。"""
        asyncio.run(_publish_users(storage._client, "alice", "a-leader", ["bob"]))
        access = self._access(storage, policy)
        views = asyncio.run(access.list_resource("bob", ResourceKind.AGENT))
        by_id = {v.id: v for v in views}
        assert set(by_id) == {"b-agent", "a-leader"}
        assert by_id["b-agent"].editable is True
        assert by_id["a-leader"].editable is False, "共享条目必须只读"
        assert by_id["a-leader"].data.name == "主理人（大A）"

    def test_team_workers_not_shareable(self, storage, policy):
        """source=team 的成员随团队生命周期，不得出现在共享列表。"""
        asyncio.run(
            _publish_users(storage._client, "alice", "a-team-worker", ["bob"])
        )
        access = self._access(storage, policy)
        views = asyncio.run(access.list_resource("bob", ResourceKind.AGENT))
        assert all(v.id != "a-team-worker" for v in views)

    def test_resolve_agent_granted_viewer(self, storage, policy):
        """会话/聊天链路：被授权账号可以 resolve 共享智能体。"""
        asyncio.run(_publish_users(storage._client, "alice", "a-leader", ["bob"]))
        access = self._access(storage, policy)
        rec = asyncio.run(access.resolve_agent("bob", "a-leader"))
        assert rec is not None and rec.user_id == "alice"

    def test_resolve_agent_ungranted_404(self, storage, policy):
        """未授权账号 resolve → 404（会话创建被拒）。"""
        from fastapi import HTTPException

        asyncio.run(_publish_users(storage._client, "alice", "a-leader", ["bob"]))
        access = self._access(storage, policy)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(access.resolve_agent("carol", "a-leader"))
        assert ei.value.status_code == 404

    def test_can_edit_false_for_shared(self, storage, policy):
        """编辑权判定：共享只读（can_edit=False），owner 恒 True。"""
        asyncio.run(_publish_users(storage._client, "alice", "a-leader", ["bob"]))
        assert asyncio.run(
            policy.can_edit("bob", ResourceKind.AGENT, "alice", "a-leader", storage)
        ) is False
        assert asyncio.run(
            policy.can_edit("alice", ResourceKind.AGENT, "alice", "a-leader", storage)
        ) is True


# ---------------------------------------------------------------- 管理 API

class TestShareApi:
    """发布/取消/我的发布 端点。"""

    def test_publish_users_mode(self, storage, api_app):
        client = _client_of(api_app)
        r = client.put(
            "/agent-share/a-leader",
            json={"mode": "users", "users": ["bob", "carol"]},
            headers={"X-User-ID": "alice"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "users"
        assert body["users"] == ["bob", "carol"]  # 排序去重

        # Redis 结构正确
        raw = asyncio.run(
            storage._client.get("agentforge:share:agent:a-leader")
        )
        rec = json.loads(raw)
        assert rec["owner"] == "alice"
        assert asyncio.run(
            storage._client.sismember("agentforge:share:to:bob", "a-leader")
        )

    def test_publish_public_mode(self, storage, api_app):
        client = _client_of(api_app)
        r = client.put(
            "/agent-share/a-member",
            json={"mode": "public", "users": []},
            headers={"X-User-ID": "alice"},
        )
        assert r.status_code == 200
        assert asyncio.run(
            storage._client.sismember("agentforge:share:public", "a-member")
        )

    def test_publish_requires_ownership(self, api_app):
        """替他人发布 → 404（不泄漏存在性）。"""
        client = _client_of(api_app)
        r = client.put(
            "/agent-share/b-agent",  # bob 的智能体
            json={"mode": "public"},
            headers={"X-User-ID": "alice"},
        )
        assert r.status_code == 404

    def test_publish_unknown_agent_404(self, api_app):
        client = _client_of(api_app)
        r = client.put(
            "/agent-share/nope",
            json={"mode": "public"},
            headers={"X-User-ID": "alice"},
        )
        assert r.status_code == 404

    def test_publish_team_worker_rejected(self, api_app):
        """团队成员不开放单独共享（随团队生命周期）。"""
        client = _client_of(api_app)
        r = client.put(
            "/agent-share/a-team-worker",
            json={"mode": "public"},
            headers={"X-User-ID": "alice"},
        )
        assert r.status_code == 400

    def test_publish_validation(self, api_app):
        client = _client_of(api_app)
        h = {"X-User-ID": "alice"}
        # mode 非法
        assert client.put(
            "/agent-share/a-leader", json={"mode": "everyone"}, headers=h
        ).status_code == 422
        # users 模式但空列表
        assert client.put(
            "/agent-share/a-leader", json={"mode": "users", "users": []}, headers=h
        ).status_code == 422
        # 非法用户名
        assert client.put(
            "/agent-share/a-leader",
            json={"mode": "users", "users": ["bad name!"]},
            headers=h,
        ).status_code == 422

    def test_update_visibility_swaps_sets(self, storage, api_app):
        """users → public 切换：旧 to:{u} 成员清除、public 集合加入。"""
        client = _client_of(api_app)
        h = {"X-User-ID": "alice"}
        client.put(
            "/agent-share/a-leader",
            json={"mode": "users", "users": ["bob"]},
            headers=h,
        )
        client.put(
            "/agent-share/a-leader", json={"mode": "public"}, headers=h
        )
        assert not asyncio.run(
            storage._client.sismember("agentforge:share:to:bob", "a-leader")
        ), "切公开后旧的用户授权应清除"
        assert asyncio.run(
            storage._client.sismember("agentforge:share:public", "a-leader")
        )

    def test_mine_lists_only_own(self, storage, api_app):
        asyncio.run(_publish_users(storage._client, "alice", "a-leader", ["bob"]))
        # bob 也发布一个
        asyncio.run(
            storage._client.set(
                "agentforge:share:agent:b-agent",
                json.dumps({"owner": "bob", "mode": "public", "users": [],
                            "agent_name": "Bob 的智能体", "shared_at": "t"}),
            )
        )
        asyncio.run(storage._client.sadd("agentforge:share:public", "b-agent"))
        asyncio.run(storage._client.sadd("agentforge:share:owner:bob", "b-agent"))

        client = _client_of(api_app)
        body = client.get(
            "/agent-share/mine", headers={"X-User-ID": "alice"}
        ).json()
        ids = [s["agent_id"] for s in body["shares"]]
        assert ids == ["a-leader"], "只列自己的发布"
        assert body["shares"][0]["users"] == ["bob"]

    def test_unpublish_restores_private(self, storage, api_app):
        client = _client_of(api_app)
        h = {"X-User-ID": "alice"}
        client.put(
            "/agent-share/a-leader",
            json={"mode": "users", "users": ["bob"]},
            headers=h,
        )
        r = client.delete("/agent-share/a-leader", headers=h)
        assert r.status_code == 200
        assert asyncio.run(
            storage._client.exists("agentforge:share:agent:a-leader")
        ) == 0
        assert not asyncio.run(
            storage._client.sismember("agentforge:share:to:bob", "a-leader")
        )
        # 幂等：再删不报错
        assert client.delete("/agent-share/a-leader", headers=h).status_code == 200

    def test_unpublish_only_owner(self, storage, api_app):
        asyncio.run(_publish_users(storage._client, "alice", "a-leader", ["bob"]))
        client = _client_of(api_app)
        r = client.delete("/agent-share/a-leader", headers={"X-User-ID": "bob"})
        assert r.status_code == 403, "只有发布者可以取消发布"

    def test_unauthenticated_401(self, api_app):
        client = _client_of(api_app)
        assert client.get("/agent-share/mine").status_code == 401
        assert client.put(
            "/agent-share/a-leader", json={"mode": "public"}
        ).status_code == 401


# ---------------------------------------------------------------- 版本端点可见性

class TestVersionEndpointVisibility:
    """agent_version 的版本列表/详情：owner/被共享可见，他人 404。

    版本快照含提示词资产——共享上线后必须显式校验请求者身份
    （此前仅靠 agent_id 不可猜测性）。
    """

    @pytest.fixture
    def version_app(self, storage, policy, tmp_path, monkeypatch):
        """装载 agent_version_router 的 app + 临时版本文件。"""
        from app import agent_version

        monkeypatch.setattr(
            agent_version, "_versions_file", lambda: tmp_path / "versions.json"
        )
        # 预置一条版本记录
        store = agent_version.AgentVersionStore(tmp_path / "versions.json")
        store.save(
            "a-leader",
            {
                "frozen": True,
                "current_version": 1,
                "versions": [
                    {"version": 1, "created_at": "t", "label": "",
                     "data": {"name": "主理人（大A）", "system_prompt": "秘密提示词"}}
                ],
            },
        )
        app = FastAPI()
        app.state.storage = storage
        app.state.resource_access_policy = policy
        app.include_router(agent_version.agent_version_router)
        return app

    def test_owner_can_read_versions(self, version_app):
        client = _client_of(version_app)
        assert client.get(
            "/agent/a-leader/versions", headers={"X-User-ID": "alice"}
        ).status_code == 200

    def test_granted_viewer_can_read(self, storage, version_app):
        asyncio.run(_publish_users(storage._client, "alice", "a-leader", ["bob"]))
        client = _client_of(version_app)
        r = client.get(
            "/agent/a-leader/versions", headers={"X-User-ID": "bob"}
        )
        assert r.status_code == 200
        # 详情也放行
        assert client.get(
            "/agent/a-leader/versions/1", headers={"X-User-ID": "bob"}
        ).status_code == 200

    def test_stranger_gets_404(self, version_app):
        """无关账号读版本 → 404（不泄漏存在性与提示词）。"""
        client = _client_of(version_app)
        assert client.get(
            "/agent/a-leader/versions", headers={"X-User-ID": "carol"}
        ).status_code == 404
        assert client.get(
            "/agent/a-leader/versions/1", headers={"X-User-ID": "carol"}
        ).status_code == 404


# ---------------------------------------------------------------- 版本化发布

class TestVersionedPublish:
    """POST /agent-share/publish：发布 = 版本快照复制 + 共享（2026-09-08）。

    用户场景：高考志愿兵 v1 基础 → v2 文科 → v3 理科；
    发布 v2（对外名"文科志愿专家"）与 v3（"理科志愿专家"）——
    两个独立产品并存，源智能体继续迭代互不影响。
    """

    @pytest.fixture
    def pub_env(self, storage, api_app, tmp_path, monkeypatch):
        """stub 复制核心 + 版本存储（发布端点的两个外部依赖）。"""
        import app.agent_version as av_mod

        # 版本存储：v1/v2/v3 都存在（get_version 放行）
        versions = tmp_path / "versions.json"
        versions.write_text(json.dumps({
            "a-leader": {"frozen": False, "current_version": 3, "versions": [
                {"version": v, "created_at": "2026-09-08T00:00:00",
                 "label": "", "data": {"name": "高考志愿兵"}}
                for v in (1, 2, 3)
            ]},
        }), encoding="utf-8")
        monkeypatch.setenv("AGENTFORGE_AGENT_VERSIONS_FILE", str(versions))

        created = {"next": 1}

        async def fake_dup(agent_id, user_id, name, version, stg):
            new_id = f"pub{created['next']}"
            created["next"] += 1
            return {
                "agent_id": new_id, "name": name,
                "source_agent_id": agent_id, "source_version": version,
            }

        monkeypatch.setattr(av_mod, "duplicate_agent_core", fake_dup)
        return storage, _client_of(api_app)

    def _publish(self, client, version=2, name="文科志愿专家",
                 mode="users", users=("bob",)):
        return client.post(
            "/agent-share/publish",
            json={
                "agent_id": "a-leader", "version": version,
                "display_name": name, "mode": mode, "users": list(users),
            },
            headers={"X-User-ID": "alice"},
        )

    def test_publish_creates_independent_product(self, pub_env):
        """发布 v2 → 独立发布物 + 共享记录 + 溯源元数据齐备。"""
        storage, client = pub_env
        r = self._publish(client, version=2, name="文科志愿专家")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["display_name"] == "文科志愿专家"
        assert body["source_agent_id"] == "a-leader"
        assert body["source_version"] == 2
        pub_id = body["agent_id"]
        # 被共享账号授权成立（复用 v1 共享机制）
        assert asyncio.run(
            storage._client.sismember("agentforge:share:to:bob", pub_id)
        )
        # 溯源元数据落库
        raw = asyncio.run(
            storage._client.get(f"agentforge:share:pubmeta:{pub_id}")
        )
        meta = json.loads(raw)
        assert meta["source_agent_id"] == "a-leader"
        assert meta["source_version"] == 2
        assert meta["display_name"] == "文科志愿专家"

    def test_publish_two_versions_coexist(self, pub_env):
        """v2 文科 + v3 理科：两个发布物并存互不干扰。"""
        storage, client = pub_env
        r2 = self._publish(client, version=2, name="文科志愿专家")
        r3 = self._publish(client, version=3, name="理科志愿专家")
        id2, id3 = r2.json()["agent_id"], r3.json()["agent_id"]
        assert id2 != id3
        # pubs 列表两条，溯源版本正确
        pubs = client.get(
            "/agent-share/pubs", headers={"X-User-ID": "alice"},
        ).json()["publications"]
        by_name = {p["display_name"]: p for p in pubs}
        assert by_name["文科志愿专家"]["source_version"] == 2
        assert by_name["理科志愿专家"]["source_version"] == 3

    def test_publish_requires_existing_version(self, pub_env):
        _, client = pub_env
        r = self._publish(client, version=9)
        assert r.status_code == 404
        assert "v9" in r.json()["detail"]

    def test_publish_validation(self, pub_env):
        _, client = pub_env
        # mode=private 不合法（发布必共享）
        r = self._publish(client, mode="private")
        assert r.status_code == 422
        # users 模式缺账号
        r = self._publish(client, users=())
        assert r.status_code == 422
        # 对外名称为空
        r = self._publish(client, name="  ")
        assert r.status_code == 422
        # 非法账号名
        r = self._publish(client, users=("bad name!",))
        assert r.status_code == 422

    def test_publish_public_mode(self, pub_env):
        storage, client = pub_env
        r = self._publish(client, mode="public", users=[])
        assert r.status_code == 200
        pub_id = r.json()["agent_id"]
        assert asyncio.run(
            storage._client.sismember("agentforge:share:public", pub_id)
        )

    def test_pubs_lists_only_own(self, pub_env):
        """别人的发布物不出现在我的列表。"""
        _, client = pub_env
        self._publish(client)
        r = client.get(
            "/agent-share/pubs", headers={"X-User-ID": "bob"},
        )
        assert r.json()["publications"] == []

    def test_unauthorized_401(self, pub_env):
        _, client = pub_env
        # 合法 body + 无身份头 → 401
        r = client.post(
            "/agent-share/publish",
            json={
                "agent_id": "a-leader", "version": 2,
                "display_name": "x", "mode": "public",
            },
        )
        assert r.status_code == 401
