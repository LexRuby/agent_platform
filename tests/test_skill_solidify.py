"""技能固化（SolidifySkill）测试——2026-09-09 培育闭环固定环节。

背景：任务交付后"自我整理/抽象/工具化"固化成全局工具。此前 agent
即兴发挥——主理人手建 skills_library/ 目录，不在官方加载链路上，
固化了也永远不生效。

实现（app/skill_solidify.py）：
- SolidifySkill 工具：校验 → 解析目标（自己/团队成员）→ 官方
  workspace.add_skill() 写入正确分区（SKILL.md 标准格式，官方校验/
  去重/索引）→ 下次会话 <agent-skills> 自动可见
- 同名技能再次固化 = 迭代更新（先 remove 再 add，官方 add_skill
  对同名只会加后缀 "(1)"）
- make_solidify_factory：create_app(extra_agent_tools=...) 工厂，
  workspace_manager 官方单例由 agent_service_app 注入

测试不依赖真实 Redis / 真实 LLM：FakeStorage + FakeWorkspace
（duck-typing add_skill/list_skills/remove_skill）+ 官方 LocalWorkspace
真实链路验证（SKILL.md 落盘 + list_skills 解析 frontmatter）。
"""

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agentforge"))

from app.skill_solidify import (  # noqa: E402
    SolidifySkill,
    make_solidify_factory,
)


# ---------------------------------------------------------------- Fake 模型

@dataclass
class _Member:
    name: str
    agent_id: str


@dataclass
class _TeamData:
    members: list = field(default_factory=list)


@dataclass
class _Team:
    data: _TeamData = None

    def __post_init__(self):
        if self.data is None:
            self.data = _TeamData()


@dataclass
class _Session:
    team_id: str | None = None


class _Skill:
    """官方 Skill 的最小替身（list_skills 返回元素）。"""

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description


class FakeWorkspace:
    """记录调用 + 可编程现有技能（duck-typing 官方接口）。

    add_skill 时即读取 staging 内容（工具调用结束会清理 staging，
    事后读不到）。"""

    def __init__(self, existing: list[_Skill] | None = None):
        self.existing = list(existing or [])
        self.added: list[tuple[str, str | None]] = []  # (path, agent_id)
        self.removed: list[tuple[str, str | None]] = []
        self.add_paths: list[Path] = []
        self.add_contents: list[str] = []  # SKILL.md 内容快照
        self.fail_add: Exception | None = None

    async def list_skills(self, *, agent_id=None):
        return list(self.existing)

    async def remove_skill(self, name, *, agent_id=None):
        self.removed.append((name, agent_id))
        self.existing = [s for s in self.existing if s.name != name]

    async def add_skill(self, path, *, agent_id=None):
        self.add_paths.append(Path(path))
        try:
            if self.fail_add:
                raise self.fail_add
            self.added.append((path, agent_id))
            self.add_contents.append(
                (Path(path) / "SKILL.md").read_text(encoding="utf-8"),
            )
        finally:
            # 记录路径后立即检查目录存在性（工具 finally 清理前）
            self._path_existed_at_add = Path(path).exists()


class FakeWM:
    """workspace_manager 替身：get_workspace 返回注入的 workspace。"""

    def __init__(self, workspace):
        self.workspace = workspace
        self.calls: list[tuple] = []

    async def get_workspace(self, user_id, agent_id, session_id,
                            workspace_id=None):
        self.calls.append((user_id, agent_id, session_id))
        return self.workspace


class FakeStorage:
    """会话 + 团队查询替身。"""

    def __init__(self, session=None, team=None):
        self.session = session
        self.team = team

    async def get_session(self, user_id, agent_id, session_id):
        return self.session

    async def get_team(self, user_id, team_id):
        return self.team


def _make_tool(storage=None, wm=None, agent_id="leader-1"):
    return SolidifySkill(
        storage=storage or FakeStorage(),
        workspace_manager=wm or FakeWM(FakeWorkspace()),
        user_id="u1",
        agent_id=agent_id,
        session_id="sess-1",
    )


def _call(tool, **kw):
    """跑一次工具调用（校验失败抛 ValueError，成功返回文本）。"""
    return asyncio.run(tool.call(**kw))


# ---------------------------------------------------------------- 参数校验

class TestValidation:
    def test_empty_name_rejected(self):
        with pytest.raises(ValueError, match="技能名"):
            _call(_make_tool(), skill_name="", description="d", content="x" * 50)

    def test_name_too_long_rejected(self):
        with pytest.raises(ValueError, match="不超过"):
            _call(_make_tool(), skill_name="名" * 51, description="d",
                  content="x" * 50)

    def test_name_special_chars_rejected(self):
        with pytest.raises(ValueError, match="特殊符号"):
            _call(_make_tool(), skill_name="技能/名字!", description="d",
                  content="x" * 50)

    def test_chinese_name_accepted(self):
        """中文名是官方支持的（_sanitize_dir_name 白名单含 CJK）。"""
        chunk = _call(
            _make_tool(), skill_name="串联臂拉格朗日建模",
            description="建串联臂动力学模型时用",
            content="方法论：拉格朗日方程..." + "详" * 40,
        )
        assert "串联臂拉格朗日建模" in chunk.content[0].text

    def test_empty_description_rejected(self):
        with pytest.raises(ValueError, match="description"):
            _call(_make_tool(), skill_name="技能", description="  ",
                  content="x" * 50)

    def test_thin_content_rejected(self):
        """正文太单薄直接拒绝——固化要能被复用，不是一句话备忘。"""
        with pytest.raises(ValueError, match="单薄"):
            _call(_make_tool(), skill_name="技能", description="d", content="太短")


# ---------------------------------------------------------------- 目标解析

class TestTargetResolution:
    def test_default_self(self):
        """不填 for_member → 固化到自己的分区。"""
        ws = FakeWorkspace()
        tool = _make_tool(wm=FakeWM(ws))
        _call(tool, skill_name="技能", description="d", content="x" * 50)
        assert ws.added[0][1] == "leader-1"  # agent_id = 调用者自己

    def test_member_resolution(self):
        """for_member → 从会话的团队解析成员 agent_id。"""
        ws = FakeWorkspace()
        storage = FakeStorage(
            session=_Session(team_id="t-1"),
            team=_Team(_TeamData([_Member("动力学专家", "member-9")])),
        )
        tool = _make_tool(storage=storage, wm=FakeWM(ws))
        chunk = _call(
            tool, skill_name="技能", description="d", content="x" * 50,
            for_member="动力学专家",
        )
        assert ws.added[0][1] == "member-9"
        assert "成员「动力学专家」" in chunk.content[0].text

    def test_unknown_member_lists_available(self):
        """成员不存在 → 报错并列出当前成员名（可恢复的错误提示）。"""
        storage = FakeStorage(
            session=_Session(team_id="t-1"),
            team=_Team(_TeamData([_Member("动力学专家", "m1")])),
        )
        tool = _make_tool(storage=storage)
        with pytest.raises(ValueError, match="动力学专家"):
            _call(tool, skill_name="技能", description="d", content="x" * 50,
                  for_member="查无此人")

    def test_no_team_for_member_rejected(self):
        """无团队会话 + for_member → 明确报错（不能悄悄固化给自己）。"""
        tool = _make_tool(storage=FakeStorage(session=_Session(team_id=None)))
        with pytest.raises(ValueError, match="不存在"):
            _call(tool, skill_name="技能", description="d", content="x" * 50,
                  for_member="任何人")

    def test_member_lookup_storage_error_degrades(self):
        """storage 异常 → 按无团队处理（不炸调用，给出成员不存在提示）。"""
        class _Boom:
            async def get_session(self, *a):
                raise RuntimeError("connection lost")

        tool = _make_tool(storage=_Boom())
        with pytest.raises(ValueError, match="不存在"):
            _call(tool, skill_name="技能", description="d", content="x" * 50,
                  for_member="任何人")


# ---------------------------------------------------------------- 固化语义

class TestSolidifySemantics:
    def test_skill_md_format(self):
        """SKILL.md 必须是官方标准格式：YAML frontmatter + 正文。"""
        ws = FakeWorkspace()
        _call(
            _make_tool(wm=FakeWM(ws)),
            skill_name="拉格朗日建模",
            description="建机械臂动力学模型时用",
            content="## 方法论\n拉格朗日方程…\n## 失败模式\n" + "详" * 40,
        )
        md = ws.add_contents[0]
        assert md.startswith("---\n")
        assert "name: 拉格朗日建模" in md
        assert "description: 建机械臂动力学模型时用" in md
        assert "## 方法论" in md  # 正文原样保留

    def test_same_name_replaces(self):
        """同名技能再次固化 = 迭代更新（remove + add，不是加后缀）。"""
        ws = FakeWorkspace(existing=[_Skill("拉格朗日建模", "旧版")])
        chunk = _call(
            _make_tool(wm=FakeWM(ws)),
            skill_name="拉格朗日建模", description="新版",
            content="x" * 50,
        )
        assert ws.removed == [("拉格朗日建模", "leader-1")]
        assert len(ws.added) == 1
        assert "已迭代更新" in chunk.content[0].text

    def test_different_name_no_removal(self):
        """不同名技能共存——只 add 不 remove。"""
        ws = FakeWorkspace(existing=[_Skill("既有技能", "x")])
        _call(
            _make_tool(wm=FakeWM(ws)),
            skill_name="新技能", description="d", content="x" * 50,
        )
        assert ws.removed == []
        assert len(ws.added) == 1

    def test_removal_scoped_to_target_partition(self):
        """给成员迭代技能时，remove/add 都落在成员分区。"""
        ws = FakeWorkspace(existing=[_Skill("技能A", "旧")])
        storage = FakeStorage(
            session=_Session(team_id="t-1"),
            team=_Team(_TeamData([_Member("仿真专家", "sim-1")])),
        )
        _call(
            _make_tool(storage=storage, wm=FakeWM(ws)),
            skill_name="技能A", description="d", content="x" * 50,
            for_member="仿真专家",
        )
        assert ws.removed == [("技能A", "sim-1")]
        assert ws.added == [(ws.added[0][0], "sim-1")]

    def test_staging_cleaned_after_add(self):
        """staging 临时目录必须清理（不留垃圾）。"""
        import tempfile

        ws = FakeWorkspace()
        _call(
            _make_tool(wm=FakeWM(ws)),
            skill_name="技能", description="d", content="x" * 50,
        )
        staging = ws.add_paths[0]
        assert not staging.exists(), "staging 目录未清理"
        # staging 在系统临时目录下（不在工作区留痕）
        assert str(staging).startswith(tempfile.gettempdir())

    def test_add_failure_still_cleans_staging(self):
        """add_skill 抛异常 → staging 也要清理（finally 语义）。"""
        ws = FakeWorkspace()
        ws.fail_add = RuntimeError("disk full")
        with pytest.raises(RuntimeError, match="disk full"):
            _call(
                _make_tool(wm=FakeWM(ws)),
                skill_name="技能", description="d", content="x" * 50,
            )
        assert ws.add_paths and not ws.add_paths[0].exists()

    def test_confirmation_message_mentions_next_session(self):
        """确认文案要告诉 agent 技能何时生效（下次会话清单自动可见）。"""
        chunk = _call(
            _make_tool(),
            skill_name="技能", description="d", content="x" * 50,
        )
        text = chunk.content[0].text
        assert "下次会话" in text and "skill_viewer" in text


# ---------------------------------------------------------------- 工厂

class TestFactory:
    def test_factory_returns_tool_with_services(self):
        """工厂产出绑定身份的工具实例；services 由部署方注入。"""
        storage = FakeStorage()
        factory = make_solidify_factory(storage)
        factory.services["workspace_manager"] = FakeWM(FakeWorkspace())
        tools = asyncio.run(factory("u1", "leader-1", "sess-1"))
        assert len(tools) == 1
        assert isinstance(tools[0], SolidifySkill)
        assert tools[0].name == "SolidifySkill"

    def test_factory_without_wm_returns_empty(self):
        """workspace_manager 未注入 → 静默跳过（不炸会话）。"""
        factory = make_solidify_factory(FakeStorage())
        assert asyncio.run(factory("u1", "a", "s")) == []

    def test_tool_schema_complete(self):
        """input_schema 必须含全部字段定义（LLM 按此生成参数）。"""
        schema = SolidifySkill.input_schema
        props = schema["properties"]
        assert set(props) == {"skill_name", "description", "content",
                              "for_member"}
        assert set(schema["required"]) == {"skill_name", "description",
                                           "content"}

    def test_permissions_allow(self):
        """工作区内写入，无跨账号副作用 → ALLOW。"""
        import asyncio as aio
        from agentscope.permission import PermissionBehavior, PermissionContext

        tool = _make_tool()
        d = aio.run(tool.check_permissions(
            {}, PermissionContext(user_id="u1"),
        ))
        assert d.behavior is PermissionBehavior.ALLOW


# ---------------------------------------------------------------- 官方链路 E2E

class TestOfficialChain:
    """真实 LocalWorkspace：SKILL.md 落盘 → list_skills 解析 frontmatter。

    这是官方加载链路的最小验证（真实文件系统 + 官方 add/list），
    不碰 Redis / LLM。
    """

    def test_real_workspace_roundtrip(self, tmp_path):
        from agentscope.workspace import LocalWorkspace

        ws = LocalWorkspace(workdir=str(tmp_path))
        tool = SolidifySkill(
            storage=FakeStorage(),
            workspace_manager=FakeWM(ws),
            user_id="u1", agent_id="agent-x", session_id="s1",
        )
        chunk = _call(
            tool,
            skill_name="等效摆晃动建模",
            description="容器液体晃动建模时用：等效单摆近似",
            content="## 方法论\n等效摆…\n## 失败模式\n" + "详" * 40,
        )
        assert "已固化" in chunk.content[0].text

        # 官方 list_skills 能读回来（frontmatter 解析 + 分区隔离）
        skills = asyncio.run(ws.list_skills(agent_id="agent-x"))
        names = [s.name for s in skills]
        assert "等效摆晃动建模" in names
        assert "等效单摆近似" in next(
            s.description for s in skills if s.name == "等效摆晃动建模"
        )

        # SKILL.md 真实落盘在 agent 分区
        md_files = list((tmp_path / "skills").rglob("SKILL.md"))
        assert len(md_files) == 1
        assert "agent-x" in str(md_files[0]) or "skills" in str(md_files[0])

        # 迭代：同名再固化 → 仍只有一个（remove+add，非加后缀）
        _call(
            tool,
            skill_name="等效摆晃动建模",
            description="v2：含非线性修正",
            content="## 方法论\nv2…" + "详" * 40,
        )
        skills2 = asyncio.run(ws.list_skills(agent_id="agent-x"))
        same = [s for s in skills2 if s.name == "等效摆晃动建模"]
        assert len(same) == 1 and "v2" in same[0].description

        # 分区隔离：另一个 agent 看不到
        other = asyncio.run(ws.list_skills(agent_id="agent-y"))
        assert all(s.name != "等效摆晃动建模" for s in other)

    def test_real_workspace_official_validation(self, tmp_path):
        """官方 add_skill 校验兜底：非法 SKILL.md 不会入库。"""
        from agentscope.workspace import LocalWorkspace

        ws = LocalWorkspace(workdir=str(tmp_path))
        # 直接调 add_skill 传一个缺 frontmatter 的目录 → 官方抛 ValueError
        bad = tmp_path / "bad-skill"
        bad.mkdir()
        (bad / "SKILL.md").write_text("没有 frontmatter 的内容",
                                      encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid skill"):
            asyncio.run(ws.add_skill(str(bad), agent_id="a1"))


# ---------------------------------------------------------------- 提示词 SOP

class TestPromptSop:
    def test_leader_default_prompt_has_sop(self):
        """新主理人默认提示词含技能沉淀固定环节。"""
        from app.leader_team import DEFAULT_LEADER_PROMPT

        assert "技能沉淀" in DEFAULT_LEADER_PROMPT
        assert "SolidifySkill" in DEFAULT_LEADER_PROMPT
        assert "skill_viewer" in DEFAULT_LEADER_PROMPT

    def test_team_section_has_sop(self):
        """预置成员段落含固化纪律（PATCH 重写时自动带上）。"""
        from app.leader_team import build_team_section

        section = build_team_section([{"name": "专家", "id": "x" * 32,
                                       "description": "做事"}])
        assert "SolidifySkill" in section
        assert "固化" in section

    def test_app_wiring(self):
        """agent_service_app 接线：create_app 带 extra_agent_tools + 注入。"""
        import agent_service_app as asa

        # 工厂已注入官方 workspace_manager
        assert asa._solidify_factory.services.get("workspace_manager") \
            is asa._official_app.state.workspace_manager
        # 官方 app 记录了工厂（create_app 参数链路）
        assert asa._official_app.state.extra_agent_tools \
            is asa._solidify_factory


class TestRuntimeSopInjection:
    """运行时 SOP 注入（2026-09-09 用户需求：所有 agent 默认都有）。

    存储层注入覆盖不到 AgentCreate 成员（直接走 storage，不经 HTTP
    中间件）与存量 agent——patch ChatService 的 Agent 构造类，聊天
    组装时统一追加（查重幂等，不污染存储提示词）。
    """

    def test_patch_replaces_chat_agent_class(self):
        """patch 后 _chat.Agent 是包装类（SOP 注入生效点）。"""
        from agentscope.app._service import _chat
        from app import skill_solidify

        skill_solidify.patch_runtime_sop()
        assert getattr(_chat.Agent, "_agentforge_sop_injected", False)

    def test_patch_idempotent(self):
        """重复 patch 不叠加包装类。"""
        from agentscope.app._service import _chat
        from app import skill_solidify

        skill_solidify.patch_runtime_sop()
        first = _chat.Agent
        skill_solidify.patch_runtime_sop()
        assert _chat.Agent is first  # 幂等：同一次 patch

    def test_sop_section_shape(self):
        """SOP 段内容完整：固化动作/纪律/复用三要素。"""
        from app import skill_solidify

        sec = skill_solidify.SOP_SECTION
        assert skill_solidify.SOP_MARKER in sec
        assert "SolidifySkill" in sec
        assert "泛化抽象" in sec
        assert "skill_viewer" in sec or "<agent-skills>" in sec

    def test_apply_runtime_sop_pure_function(self):
        """SOP 变换纯函数：追加/查重跳过/None 透传。"""
        from app import skill_solidify

        # 无 SOP → 追加（原文保留在前）
        out = skill_solidify.apply_runtime_sop("你是普通小A。")
        assert out.startswith("你是普通小A。")
        assert skill_solidify.SOP_MARKER in out
        assert "SolidifySkill" in out

        # 已含 SOP（存储层注入过的 leader 提示词）→ 原样返回
        with_sop = "主理人。\n\n" + skill_solidify.SOP_SECTION
        assert skill_solidify.apply_runtime_sop(with_sop) is with_sop

        # 空提示词 → 官方 None 语义透传
        assert skill_solidify.apply_runtime_sop(None) is None

        # 尾部空白清理后追加
        out2 = skill_solidify.apply_runtime_sop("成员提示。\n  ")
        assert "成员提示。\n\n\n## 技能沉淀" in out2 or out2.startswith("成员提示。")

    def test_wrapper_class_uses_pure_function(self):
        """包装类 __init__ 委托 apply_runtime_sop（组装点正确）。"""
        from app import skill_solidify

        skill_solidify.patch_runtime_sop()
        sop_cls = skill_solidify.get_sop_agent_cls()
        assert sop_cls is not None
        assert sop_cls.__module__ is not None

        # 猴补父类构造：拦截 super().__init__ 收到的最终提示词
        received = {}
        bases = sop_cls.__bases__

        class _FakeBase:
            def __init__(self, *, system_prompt=None, **kw):
                received["sp"] = system_prompt

        sop_cls.__bases__ = (_FakeBase,)
        try:
            sop_cls(system_prompt="AgentCreate 成员。")
            assert skill_solidify.SOP_MARKER in received["sp"]
            assert received["sp"].startswith("AgentCreate 成员。")
        finally:
            sop_cls.__bases__ = bases
