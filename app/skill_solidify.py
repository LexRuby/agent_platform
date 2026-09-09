"""技能固化（SolidifySkill）：把实战验证过的工作方法固化为可复用技能。

背景（2026-09-09 用户反馈）：任务交付后"自我整理、抽象、工具化"是
培育闭环的固定环节，不能靠 agent 即兴发挥——主理人曾在工作区手建
skills_library/ 目录，完全不在官方加载链路上，固化了也永远不生效。

正确机制（全部走官方链路，零私有格式）：
- 技能标准格式：``skills/{agent 分区}/{技能名}/SKILL.md`` + YAML
  frontmatter（name/description 必填）；
- 写入走 ``workspace.add_skill()``：校验 frontmatter、SHA-256 去重、
  名字冲突自动加后缀、写入正确分区、刷新 ``.index``；
- 加载走 ``workspace.list_skills()``：每次会话组装 toolkit 时自动把
  技能清单注入 system prompt（<agent-skills> 节），agent 用
  skill_viewer 按需读全文（渐进式披露）——固化后下次会话自动可用，
  无需任何配置。

固定环节（SOP）：任务交付且用户满意后，主理人主动复盘本会话验证过
的方法，用本工具固化（给成员固化的用 for_member 指定）；新任务开始
前先查 <agent-skills> 清单、用 skill_viewer 读已有技能，避免重建。

接入：``create_app(extra_agent_tools=...)``（官方扩展点，所有 agent
的 toolkit 都会带上本工具）。workspace_manager 在 create_app 返回后
由 agent_service_app 注入（官方单例管理器，不能自建）。
"""

import logging
import re
import shutil
import tempfile
from pathlib import Path

from agentscope.message import TextBlock
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk

_logger = logging.getLogger("agentforge.skill_solidify")

# frontmatter name 兼做目录名与 agent 可见名（官方 _sanitize_dir_name
# 允许 CJK/字母/数字/-/_）；限长防目录名失控
_NAME_MAX = 50
_DESC_MAX = 300
_NAME_RE = re.compile(r"^[\w一-鿿-][\w一-鿿- ]*$")


class SolidifySkill(ToolBase):
    """把验证过的工作方法固化为官方 skills 分区里的 SKILL.md。

    面向 agent 的固化纪律（写在 description 里，任何 agent 可见）：
    泛化抽象（不绑定具体项目参数）、结构完整（何时用/方法论/步骤/
    模板/边界/失败模式）、只固化实战验证过的内容。
    """

    name = "SolidifySkill"
    description = (
        "把实战验证过的工作方法固化为可复用技能（官方 skills 体系，"
        "下次会话自动出现在技能清单里）。这是任务交付后的固定环节："
        "用户满意后主动复盘固化；新任务开始前先查 <agent-skills> 清单"
        "并用 skill_viewer 读已有技能，避免重复造轮子。固化纪律："
        "1) 泛化——剥离项目特定参数（尺寸/日期/具体数值），保留可迁移"
        "的方法论本质；2) 结构——description 写清什么场景该用；正文含"
        "方法论、操作步骤、公式或代码模板、适用边界、已知失败模式；"
        "3) 只固化实战验证过的结论，不固化推测。主理人可给团队成员"
        "固化（for_member 填成员名）。同名技能再次固化 = 迭代更新。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": (
                    "技能名（中英文均可，如「串联臂拉格朗日建模」或"
                    "「lagrange-serial-arm-modeling」）。用于技能清单"
                    "展示与目录名。"
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "一句话说清什么场景该用这个技能、解决什么问题"
                    "（泛化描述，不绑定具体项目）。"
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "SKILL.md 正文（markdown，不含 frontmatter）："
                    "方法论、操作步骤、公式/代码模板、适用边界、"
                    "已知失败模式、实战验证结论。"
                ),
            },
            "for_member": {
                "type": "string",
                "description": (
                    "（可选）固化给哪个团队成员——填成员名。不填 = "
                    "固化给自己。主理人复盘团队协作后按成员专长分别固化。"
                ),
            },
        },
        "required": ["skill_name", "description", "content"],
    }
    is_concurrency_safe = False
    is_read_only = False

    def __init__(
        self,
        storage,
        workspace_manager,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> None:
        super().__init__()
        self._storage = storage
        self._wm = workspace_manager
        self._user_id = user_id
        self._agent_id = agent_id
        self._session_id = session_id

    async def check_permissions(
        self,
        tool_input: dict,
        context: PermissionContext,
    ) -> PermissionDecision:
        # 工作区内的技能写入，无跨账号副作用
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Skill solidification into own workspace.",
        )

    async def _resolve_target(self, for_member: str | None) -> tuple[str, str]:
        """for_member 成员名 → (target_agent_id, 名字回显)；None → 自己。"""
        if not for_member:
            return self._agent_id, ""
        try:
            session = await self._storage.get_session(
                self._user_id, self._agent_id, self._session_id,
            )
            team_id = getattr(session, "team_id", None) if session else None
            team = (
                await self._storage.get_team(self._user_id, team_id)
                if team_id else None
            )
            members = getattr(getattr(team, "data", None), "members", None) or []
        except Exception as e:  # noqa: BLE001 — storage 异常按无团队处理
            _logger.warning("SolidifySkill 团队解析失败: %s", e)
            members = []
        for m in members:
            m_name = m.get("name") if isinstance(m, dict) else getattr(m, "name", "")
            m_aid = (
                m.get("agent_id") if isinstance(m, dict)
                else getattr(m, "agent_id", "")
            )
            if m_name == for_member and m_aid:
                return m_aid, m_name
        names = [
            m.get("name") if isinstance(m, dict) else getattr(m, "name", "")
            for m in members
        ]
        raise ValueError(
            f"团队成员「{for_member}」不存在（当前成员："
            f"{('、'.join(n for n in names if n)) or '无'}）。"
            f"不填 for_member 则固化给自己。",
        )

    async def call(self, **kwargs) -> ToolChunk:
        skill_name = (kwargs.get("skill_name") or "").strip()
        description = (kwargs.get("description") or "").strip()
        content = (kwargs.get("content") or "").strip()
        for_member = (kwargs.get("for_member") or "").strip() or None

        # ── 参数校验（比官方 pydantic 报错更早、中文提示）──
        if not skill_name or len(skill_name) > _NAME_MAX:
            raise ValueError(
                f"技能名不能为空且不超过 {_NAME_MAX} 字：{skill_name!r}",
            )
        if not _NAME_RE.match(skill_name):
            raise ValueError(
                "技能名只能含中文/字母/数字/空格/-/_，"
                f"不接受特殊符号：{skill_name!r}",
            )
        if not description or len(description) > _DESC_MAX:
            raise ValueError(
                f"description 不能为空且不超过 {_DESC_MAX} 字"
                "（说清什么场景该用这个技能）",
            )
        if len(content) < 30:
            raise ValueError(
                "content 太单薄（<30 字）——技能正文要含方法论/步骤/"
                "模板/边界，才能被复用",
            )

        target_id, member_label = await self._resolve_target(for_member)
        workspace = await self._wm.get_workspace(
            self._user_id, self._agent_id, self._session_id,
        )

        # ── 迭代语义：同名技能 = 更新（官方 add_skill 对同名只会加
        #    后缀 "(1)"，迭代场景必须先移除旧版再装新版）──
        replaced = False
        try:
            existing = await workspace.list_skills(agent_id=target_id)
        except Exception as e:  # noqa: BLE001 — 列举失败不阻断固化
            _logger.warning("SolidifySkill list_skills 失败: %s", e)
            existing = []
        for s in existing or []:
            if s.name == skill_name:
                await workspace.remove_skill(s.name, agent_id=target_id)
                replaced = True

        # ── 组装 SKILL.md（官方标准：YAML frontmatter name/description
        #    必填，正文 markdown）→ 临时目录 → add_skill（官方校验/
        #    去重/分区/索引全走官方链路）──
        skill_md = (
            "---\n"
            f"name: {skill_name}\n"
            f"description: {description}\n"
            "---\n\n"
            f"{content}\n"
        )
        staging = Path(tempfile.mkdtemp(prefix="solidify-"))
        try:
            (staging / "SKILL.md").write_text(skill_md, encoding="utf-8")
            await workspace.add_skill(str(staging), agent_id=target_id)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        owner = f"成员「{member_label}」" if member_label else "你自己"
        action = "已迭代更新" if replaced else "已固化"
        msg = (
            f"技能「{skill_name}」{action}到 {owner} 的技能库"
            f"（官方 skills 分区）。下次会话自动出现在 <agent-skills> "
            f"清单中，用 skill_viewer 可读全文；本会话内容你已掌握，"
            f"可直接引用。"
        )
        return ToolChunk(content=[TextBlock(text=msg)])


def make_solidify_factory(storage):
    """构造 ``create_app(extra_agent_tools=...)`` 工厂。

    workspace_manager 不能自建（官方单例管理器，含缓存与生命周期），
    由 agent_service_app 在 create_app 返回后注入 ``factory.services``。
    工厂在会话聊天时才被调用，届时必然已注入。
    """

    services: dict = {}

    async def factory(
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> list[ToolBase]:
        wm = services.get("workspace_manager")
        if wm is None:  # 未注入（异常部署）——静默跳过比炸会话好
            _logger.error("SolidifySkill 未注入 workspace_manager，跳过装配")
            return []
        return [
            SolidifySkill(
                storage=storage,
                workspace_manager=wm,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
            ),
        ]

    factory.services = services  # type: ignore[attr-defined]
    return factory
