"""团队删除确认 + 软解散（2026-09-07 培养资产保护，双层防护）。

产品背景（用户 2026-09-07）："大A 只是 Team 的入口，培养好的
大A+Team 是要完整绑定开放给别人用的产品资产——完全不能接受
随便就把 team 删了，删除一定要我确认。"

官方 ``TeamDelete`` 是 LLM 自主工具 + 全灭式级联（created 成员的
agent+session 物理删除、invited 成员团队 session 删除、team 记录
删除）。两层问题：LLM 不该有自主删除权；就算删除也不该毁掉培养
资产。

**第一层：强制用户确认**。patch ``TeamDelete.check_permissions`` 返回
bypass-immune 的 ASK——DEFAULT / ACCEPT_EDITS 模式下必然弹出用户
确认卡（官方 ConfirmCard），用户"始终允许"的规则也压不住
（bypass-immune 契约）；DONT_ASK（无人值守）转为 DENY；EXPLORE
本就 DENY；BYPASS 是用户显式选择的完全信任模式，按框架契约放行
（并由第二层兜底）。

**第二层：软解散**。patch ``SessionService.delete_team``——确认后
实际执行时：取消成员正在运行的任务（cancel_session_run，不删
任何记录）→ 清 leader session 的 team_id（view.team 判定解散，
团队工具随之失效）→ 成员 agent / 成员团队 session / team 记录
**全部保留**（培养资产：上下文、产出、介入对话能力）。

注意：此前版本 patch 的是 ``RedisStorage.delete_team``——但
``SessionService.delete_team`` 在调 storage 之前就先对每个成员执行
``delete_agent`` / ``delete_session``（物理删除），storage 层 patch
拦得太晚。本版改为 patch service 层（TeamDelete 工具的唯一执行
路径），storage 层级联（用户删 leader 会话时的连带清理）不受影响。
"""

import logging
from typing import Any

from fastapi import APIRouter, Request

_logger = logging.getLogger("agentforge.team_preserve")

team_history_router = APIRouter(tags=["agentforge"])


def patch_team_protection() -> None:
    """挂载双层防护：TeamDelete 强制确认 + delete_team 软解散。"""
    _patch_team_delete_permission()
    _patch_delete_team_service()


def _patch_team_delete_permission() -> None:
    """TeamDelete.check_permissions → bypass-immune ASK（强制用户确认）。"""
    from agentscope.app._tool._team_delete import TeamDelete
    from agentscope.permission import PermissionBehavior, PermissionDecision

    async def require_user_confirm(
        self: Any,
        tool_input: dict[str, Any],
        context: Any,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message=(
                "主理人请求解散团队。团队与成员是培养资产（上下文与"
                "产出将保留），解散后团队停止运行。是否确认？"
            ),
            decision_reason="agentforge: 团队删除必须经用户确认（培养资产保护）",
            bypass_immune=True,
        )

    TeamDelete.check_permissions = require_user_confirm  # type: ignore[method-assign]
    _logger.info("已 patch TeamDelete.check_permissions → 强制用户确认")


def _patch_delete_team_service() -> None:
    """SessionService.delete_team → 软解散（保留全部培养资产）。

    成员运行中的任务取消（避免僵尸运行），记录一律不删；
    leader session 的 team_id 清空（view.team 判定解散）。
    """
    from agentscope.app._service._session import SessionService

    async def soft_delete_team(self: Any, user_id: str, team_id: str) -> bool:
        team = await self._storage.get_team(user_id, team_id)
        if team is None:
            return False

        # 成员清单：与官方 _ensure_team_members 同源（team.data.members）
        members = getattr(getattr(team, "data", None), "members", None) or []
        for m in members:
            m_session = m.get("session_id") if isinstance(m, dict) else m.session_id
            m_name = (
                m.get("agent_id", "")[:8]
                if isinstance(m, dict)
                else str(getattr(m, "agent_id", ""))[:8]
            )
            if m_session:
                try:
                    await self.cancel_session_run(m_session)
                except Exception:  # noqa: BLE001 — 成员可能已停，尽力而为
                    _logger.exception(
                        "软解散: 取消成员运行失败 team=%s member=%s",
                        team_id,
                        m_name,
                    )

        # 解除 leader 绑定：view.team 判定失效 = 团队解散（工具随之不可用）
        await self._storage.set_session_team_id(user_id, team.session_id, None)

        _logger.info(
            "团队软解散（确认后执行，资产保留）: team=%s members=%d",
            team_id,
            len(members),
        )
        return True

    SessionService.delete_team = soft_delete_team  # type: ignore[method-assign]
    _logger.info("已 patch SessionService.delete_team → 软解散（保留培养资产）")


@team_history_router.get("/team-sessions/{leader_session_id}")
async def get_team_sessions(leader_session_id: str, request: Request) -> dict:
    """主理会话 → 历次团队成员 session 映射（含已解散团队）。

    返回 ``{team_id, name, dissolved, members:
    [{agent_id, agent_name, session_id, role}]}`` 列表。前端据此把
    "进入会话迭代"定位到成员在团队任务中的会话。
    """
    storage = request.app.state.storage
    user_id = request.headers.get("X-User-ID", "")
    teams = await storage.list_teams(user_id)
    result = []
    for team in teams:
        if team.session_id != leader_session_id:
            continue
        members_out = []
        for m in getattr(getattr(team, "data", None), "members", None) or []:
            # 官方 TeamMember 是 pydantic 模型（属性访问）；测试/旧数据
            # 可能是 dict——两者都兼容
            agent_id = m.get("agent_id") if isinstance(m, dict) else m.agent_id
            m_session = m.get("session_id") if isinstance(m, dict) else m.session_id
            m_role = m.get("role") if isinstance(m, dict) else m.role
            agent = await storage.get_agent(user_id, agent_id)
            agent_data = getattr(agent, "data", None)
            agent_name = getattr(agent_data, "name", "") if agent_data else ""
            members_out.append(
                {
                    "agent_id": agent_id,
                    "agent_name": agent_name or agent_id[:8],
                    "session_id": m_session,
                    "role": m_role,
                }
            )
        # 解散判定：leader session 的 team_id 已被软解散清空 → dissolved
        leader_session = await storage.get_session(user_id, team.leader_agent_id, team.session_id)
        dissolved = leader_session is None or leader_session.team_id != team.id
        team_data = getattr(team, "data", None)
        result.append(
            {
                "team_id": team.id,
                "name": getattr(team_data, "name", "") if team_data else "",
                "dissolved": dissolved,
                "members": members_out,
            }
        )
    # 多次组队按创建时间正序
    result.sort(key=lambda t: t["team_id"])
    return {"teams": result}
