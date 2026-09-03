"""生产中间件栈整链集成测试：复刻 agent_service_app.py 的真实组装顺序。

背景：各中间件单测全绿，但 2026-09-03 的"创建 leader 落回 member"事故表明
层间组合（响应契约、路径匹配、body 消费重建）才是事故高发区。本文件把
**与生产完全一致的包装顺序**整链拉起，跑用户真实使用序列：

    AuthMiddleware(
        PromptTemplateSchemaMiddleware(
            LeaderTeamMiddleware(AgentTypeMiddleware(inner_app)),
        ),
    )

覆盖要点：
- 未登录访问 /agent/ 整链 401（鉴权在最外层，不漏）
- 登录后创建带成员的主理人 → GET 回读：类型 + 名单 + 提示词注入三件事
- POST /agent/recommend-members 不被 AgentType/LeaderTeam 的路径匹配吞掉
  （/agent/recommend-members 会被 _match 当作 id="recommend-members"，
   必须验证整链下该端点仍正确路由到 router）
- GET /agent/schema/v2：prompt_templates 与 agent_type 两个 schema 注入
  中间件叠加不冲突
- 团队封档 API（/team-archive*）挂在内层，整链下可访问且受鉴权保护

内层 mock 一律经 tests/official_contract.py 工厂构造（禁止内联结构）。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth as auth_mod
from app.agent_type import AgentTypeMiddleware, AgentTypeStore, agent_type_schema_property
from app.auth import AuthMiddleware, auth_router
from app.leader_team import LeaderTeamMiddleware, LeaderTeamStore, leader_team_router
from app.prompt_templates import PromptTemplateSchemaMiddleware
from app.team_archive import team_archive_router

from tests import official_contract as oc
from tests.helpers import login


@pytest.fixture
def stack(tmp_path, fake_redis, users_dir, monkeypatch):
    """整链环境：与生产同序的中间件栈 + 契约驱动的伪官方 app。"""
    monkeypatch.setenv("AGENTFORGE_AGENT_TYPES_FILE", str(tmp_path / "types.json"))
    monkeypatch.setenv("AGENTFORGE_LEADER_TEAMS_FILE", str(tmp_path / "teams.json"))
    monkeypatch.setenv("AGENTFORGE_TEAM_ARCHIVES_FILE", str(tmp_path / "archives.json"))

    # 提示词模板：临时目录放一个最小模板
    tpl_dir = tmp_path / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "01-测试模板.yaml").write_text(
        "name: 测试模板\ndescription: 集成测试用\ncontent: |\n  你是测试助手。\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTFORGE_PROMPT_TEMPLATES_DIR", str(tpl_dir))

    inner = FastAPI()
    db = {"next": 1, "agents": {}}

    @inner.post("/agent/")
    async def create(body: dict):
        aid = f"a{db['next']}"
        db["next"] += 1
        db["agents"][aid] = body
        return oc.post_agent_response(aid)

    @inner.get("/agent/")
    async def list_():
        return oc.list_agent_response(
            [oc.agent_item(k, v) for k, v in db["agents"].items()],
        )

    @inner.patch("/agent/{aid}")
    async def update(aid: str, body: dict):
        db["agents"][aid].update(body)
        return {"id": aid, "data": db["agents"][aid]}

    @inner.delete("/agent/{aid}")
    async def remove(aid: str):
        db["agents"].pop(aid, None)
        return {"ok": True}

    @inner.get("/agent/schema/v2")
    async def schema_v2():
        # 官方真实形态经契约工厂构造：{"schema": {"properties": {...}}}
        return oc.schema_v2_response({
            "name": {"type": "string"},
            "system_prompt": {"type": "string"},
        })

    # 生产中 router 挂在官方 app 上（AuthMiddleware 内层）
    inner.include_router(leader_team_router)
    inner.include_router(team_archive_router)
    inner.include_router(auth_router)

    # 与 agent_service_app.py 完全一致的包装顺序
    app = AuthMiddleware(
        PromptTemplateSchemaMiddleware(
            LeaderTeamMiddleware(AgentTypeMiddleware(inner)),
        ),
    )
    ts = AgentTypeStore(str(tmp_path / "types.json"))
    ls = LeaderTeamStore(str(tmp_path / "teams.json"))
    return TestClient(app), ts, ls, db


class TestAuthGate:
    """鉴权最外层：整链任何业务端点未登录都必须 401。"""

    def test_agent_list_requires_login(self, stack):
        client, *_ = stack
        assert client.get("/agent/").status_code == 401

    def test_agent_create_requires_login(self, stack):
        client, *_ = stack
        r = client.post("/agent/", json={"name": "x"})
        assert r.status_code == 401

    def test_recommend_requires_login(self, stack):
        client, *_ = stack
        r = client.post("/agent/recommend-members", json={})
        assert r.status_code == 401

    def test_team_archive_requires_login(self, stack):
        client, *_ = stack
        assert client.get("/team-archive").status_code == 401
        r = client.post("/team-archive/summarize", json={"session_id": "s", "agent_id": "a"})
        assert r.status_code == 401

    def test_login_then_list_ok(self, stack):
        client, *_ = stack
        login(client)
        assert client.get("/agent/").status_code == 200


class TestFullUserJourney:
    """登录用户的完整使用序列（真实调用顺序）。"""

    def test_create_leader_with_members_through_stack(self, stack):
        client, ts, ls, db = stack
        login(client)
        # 1. 建成员
        m1 = client.post(
            "/agent/", json={"name": "高考志愿兵", "agent_type": "member"},
        ).json()["agent_id"]
        # 2. 建主理人（带成员）——三处叠加层同时生效
        lid = client.post("/agent/", json={
            "name": "高考主理人", "agent_type": "leader",
            "system_prompt": "你是主理人。",
            "team_members": [
                {"id": m1, "name": "高考志愿兵", "description": "志愿专家"},
            ],
        }).json()["agent_id"]
        # 3. 回读
        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        leader = agents[lid]
        assert leader["agent_type"] == "leader"
        assert leader["team_members"] == [m1]
        assert "- 高考志愿兵：志愿专家" in leader["data"]["system_prompt"]
        # 4. 编辑清空名单
        client.patch(f"/agent/{lid}", json={
            "system_prompt": "你是主理人。\n\n## 预置团队成员\n- 高考志愿兵：志愿专家",
            "team_members": [],
        })
        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        assert "team_members" not in agents[lid]
        assert "## 预置团队成员" not in agents[lid]["data"]["system_prompt"]

    def test_recommend_endpoint_reachable_through_stack(self, stack):
        """recommend-members 必须穿过两层路径匹配中间件到达 router。

        空输入路径不调 LLM，返回 empty-input——若被任一中间件吞掉
        （当作 /agent/{id} 处理），这里会 404/405。
        """
        client, *_ = stack
        login(client)
        r = client.post("/agent/recommend-members", json={})
        assert r.status_code == 200
        assert r.json() == {
            "recommendations": [], "fallback": False, "reason": "empty-input",
        }

    def test_schema_v2_double_injection(self, stack):
        """prompt_templates 与 agent_type 两个注入不冲突。"""
        client, *_ = stack
        login(client)
        r = client.get("/agent/schema/v2")
        assert r.status_code == 200
        props = r.json()["schema"]["properties"]
        # agent_type 枚举注入
        assert props["agent_type"]["enum"] == ["leader", "member"]
        # 提示词模板注入到 system_prompt
        tpls = props["system_prompt"].get("prompt_templates")
        assert tpls and tpls[0]["name"] == "测试模板"

    def test_team_archive_list_through_stack(self, stack):
        client, *_ = stack
        login(client)
        r = client.get("/team-archive")
        assert r.status_code == 200
        assert r.json() == []

    def test_forged_user_id_overridden(self, stack):
        """伪造 X-User-ID 不能绕过鉴权（会话身份优先）。"""
        client, *_ = stack
        # 未登录 + 伪造头 → 仍然 401
        r = client.get("/agent/", headers={"X-User-ID": "admin"})
        assert r.status_code == 401
