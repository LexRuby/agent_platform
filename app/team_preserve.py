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

**第三层：会话删除守卫（2026-09-08 事故修复）**。patch
``RedisStorage.delete_session``——用户删除"持有团队调度权的会话"
（team.session_id 指向它：原生 leader 或 fork 移交后的分支）时，
官方 storage 级联会判定"删 leader → 解散团队"并**物理删除全部
created 成员 agent + 成员会话 + team 记录**——绕过第二层（第二层
只拦 TeamDelete 工具的 service 路径）。事故时间线：fork 分支持有
调度权 → 用户清理该分支 → 团队全灭（5 个专家 agent 不可逆丢失）。
守卫语义：删除前把调度权移交给同团队其他存活会话（最早创建优先，
主会话/最早 fork 源）；无其他会话才解除绑定（team 与成员资产仍
保留，软解散语义）。移交/解绑后官方级联条件（team.session_id ==
被删 id）不成立，自然跳过全灭路径。

注意：此前版本 patch 的是 ``RedisStorage.delete_team``——但
``SessionService.delete_team`` 在调 storage 之前就先对每个成员执行
``delete_agent`` / ``delete_session``（物理删除），storage 层 patch
拦得太晚。本版改为 patch service 层（TeamDelete 工具的唯一执行
路径）；storage 层级联由第三层守卫接管。
"""

import logging
from typing import Any

from fastapi import APIRouter, Request

_logger = logging.getLogger("agentforge.team_preserve")

team_history_router = APIRouter(tags=["agentforge"])


def patch_team_protection() -> None:
    """挂载三层防护：TeamDelete 强制确认 + delete_team 软解散
    + 会话删除守卫（调度权移交，防 fork 分支删除引发团队全灭）。"""
    _patch_team_delete_permission()
    _patch_delete_team_service()
    _patch_delete_session_guard()


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


# 官方原始 delete_session / 守卫实现（模块级引用：测试 monkeypatch 用）
_delete_session_orig = None
_delete_session_guarded = None


def _patch_delete_session_guard() -> None:
    """RedisStorage.delete_session → 删除前调度权移交守卫。

    官方 storage 级联：删会话时若 ``team.session_id == 被删 id``
    （该会话持有调度权）→ ``delete_team`` 全灭级联（成员 agent、
    成员会话、team 记录物理删除）。fork（team_fork.py）把调度权
    移交给分支后，删除任一分支都会命中该级联。

    守卫：删除前检查——
    - 有其他同团队会话（fork 源/兄弟分支，team_id 相同）→
      调度权移交给最早创建的那个，团队照常存活；
    - 无其他会话 → 解除被删会话的 team_id 绑定，团队与成员
      资产保留（软解散语义，dissolved 由悬空的 team.session_id
      判定兜底）。
    两分支都让官方级联条件失效，只删会话本身。
    """
    from agentscope.app.storage._redis_storage import RedisStorage

    # 幂等：重复挂载（测试 fixture + app 启动）不嵌套
    if getattr(RedisStorage.delete_session, "_agentforge_delete_guard", False):
        return
    global _delete_session_orig, _delete_session_guarded
    if _delete_session_orig is None:
        _delete_session_orig = RedisStorage.delete_session

    async def guarded_delete_session(
        self: Any,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> bool:
        try:
            record = await self.get_session(user_id, agent_id, session_id)
        except Exception:  # noqa: BLE001 — 记录读不出交给官方路径
            _logger.exception(
                "删除守卫: 读会话失败，走官方路径 session=%s", session_id,
            )
            return await _delete_session_orig(self, user_id, agent_id, session_id)
        if record is None:
            return await _delete_session_orig(self, user_id, agent_id, session_id)

        team_id = getattr(record, "team_id", None)
        if team_id:
            team = await self.get_team(user_id, team_id)
            if team is not None and team.session_id == session_id:
                # 被删会话持有调度权 → 移交而非全灭
                others = [
                    s for s in await self.list_sessions(user_id, agent_id)
                    if s.id != session_id and s.team_id == team_id
                ]
                if others:
                    # list_sessions 按 created_at 倒序 → 最后一个最早
                    target = others[-1]
                    team.session_id = target.id
                    await self.upsert_team(user_id, team)
                    _logger.info(
                        "删除守卫: 调度权会话 %s 被删，调度权移交 %s"
                        "（团队与成员保留）",
                        session_id, target.id,
                    )
                else:
                    # 无其他分支 → 解绑（防官方级联），资产保留
                    await self.set_session_team_id(user_id, session_id, None)
                    _logger.info(
                        "删除守卫: 调度权会话 %s 被删且无其他分支，"
                        "解除绑定（团队与成员资产保留）",
                        session_id,
                    )
        return await _delete_session_orig(self, user_id, agent_id, session_id)

    guarded_delete_session._agentforge_delete_guard = True  # type: ignore[attr-defined]
    _delete_session_guarded = guarded_delete_session
    RedisStorage.delete_session = guarded_delete_session  # type: ignore[method-assign]
    _logger.info("已 patch RedisStorage.delete_session → 调度权移交守卫")


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
