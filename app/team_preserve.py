"""团队解散策略 patch + 团队历史查询 API（2026-09-07 培养能力重构）。

官方 ``TeamDelete`` 是全灭式级联（delete_team）：created 成员的
agent+session 全删、invited 成员的团队 session 删除、team 记录删除。
这与"培养"产品理念冲突——成员在任务中的上下文、产出、以及"随时
介入对话继续培养"的可能性全部随解散消失（用户 2026-09-07 反馈：
点成员"进入会话迭代"看不到团队聊天内容）。

本模块启动时 monkey-patch ``RedisStorage.delete_team``：

- **保留** 成员 agent、成员团队 session（工作成果，可回看、可
  继续对话介入培养——成员 session 是标准 agent session，
  ``POST /chat`` 直接可用）
- **保留** team 记录（成员↔session 映射可追溯）
- **只清** leader session 的 ``team_id``：官方 view.team 判定依据
  session.team_id，清空即视为解散（TeamSay 等团队操作自然失效）

另提供 ``GET /team-sessions/{leaderSessionId}``：返回该主理会话
所有团队（含已解散）的成员 session 映射，供前端"进入会话迭代"
跳转到成员的**团队会话**而非空白独立会话。
"""

import logging
from typing import Any

from fastapi import APIRouter, Request

_logger = logging.getLogger("agentforge.team_preserve")

team_history_router = APIRouter(tags=["agentforge"])


def patch_delete_team() -> Any:
    """替换官方 RedisStorage.delete_team 为"软解散"。

    保留成员/会话/团队记录，仅解除 leader session 的 team 绑定。
    升级官方包后签名变化会在启动时显式报错（无静默失效）。

    Returns:
        soft_delete_team 函数对象（测试可绑定到 duck-typing 的
        FakeStorage 上复用同一实现，不依赖真实 Redis）。
    """
    from agentscope.app.storage._redis_storage import RedisStorage

    async def soft_delete_team(self: Any, user_id: str, team_id: str) -> bool:
        # 取 team 记录：不存在 → 已被旧版硬删/从未存在，无事可做
        team = await self.get_team(user_id, team_id)
        if team is None:
            return False

        # 解除 leader session 绑定：view.team 判定失效 = 团队解散。
        # 成员 agent / 成员 session / team 记录全部保留（培养资产）。
        await self.set_session_team_id(user_id, team.session_id, None)

        _logger.info(
            "团队软解散（保留成员与会话）: team=%s name=%s members=%d",
            team_id,
            getattr(getattr(team, "data", None), "name", ""),
            len(getattr(getattr(team, "data", None), "members", []) or []),
        )
        return True

    RedisStorage.delete_team = soft_delete_team  # type: ignore[method-assign]
    _logger.info("已 patch RedisStorage.delete_team → 软解散（保留培养资产）")
    return soft_delete_team


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
        # 解散判定：该 team 仍有 agent 绑定过的 leader session（team_id
        # 已被软解散清空）→ dissolved。官方硬删的团队不会出现在列表里。
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
