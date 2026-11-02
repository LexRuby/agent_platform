"""Conversation workspace lifecycle and permission defaults."""

from types import SimpleNamespace
from pathlib import Path

import pytest

from agentscope.permission import PermissionMode

from app.account_workspace import AccountWorkspaceManager
from app.session_defaults import _new_session_state, _workspace_is_referenced


@pytest.mark.asyncio
async def test_conversations_are_isolated_by_account_and_workspace(tmp_path):
    manager = AccountWorkspaceManager(str(tmp_path))
    alice = await manager.get_workspace("alice", "agent", "s1", "ws-1")
    bob = await manager.get_workspace("bob", "agent", "s2", "ws-1")
    other = await manager.get_workspace("alice", "agent", "s3", "ws-2")

    assert alice.workdir != bob.workdir
    assert alice.workdir != other.workdir
    assert "/conversations/" in alice.workdir
    assert str(tmp_path) in alice.workdir
    await manager.close_all()


@pytest.mark.asyncio
async def test_deleting_conversation_space_keeps_project_space(tmp_path):
    manager = AccountWorkspaceManager(str(tmp_path))
    temporary = await manager.get_workspace("alice", "agent", "s1", "ws-1")
    project = await manager.get_workspace(
        "alice", "agent", "s2", "project_patent",
    )
    temporary_file = temporary.workdir + "/draft.md"
    project_file = project.workdir + "/claims.md"
    with open(temporary_file, "w", encoding="utf-8") as handle:
        handle.write("temporary")
    with open(project_file, "w", encoding="utf-8") as handle:
        handle.write("persistent")

    assert await manager.delete_temporary_workspace("alice", "ws-1") is True
    assert not __import__("os").path.exists(temporary_file)
    assert await manager.delete_temporary_workspace(
        "alice", "project_patent",
    ) is False
    assert __import__("os").path.exists(project_file)
    await manager.close_all()


def test_new_sessions_accept_edits_inside_current_workspace():
    state = _new_session_state()
    assert state.permission_context.mode is PermissionMode.ACCEPT_EDITS


@pytest.mark.asyncio
async def test_reference_check_includes_team_created_agents():
    session = SimpleNamespace(config=SimpleNamespace(workspace_id="shared"))

    class Storage:
        async def list_agents(self, user_id):
            return [SimpleNamespace(id="leader")]

        async def list_teams(self, user_id):
            return [SimpleNamespace(member_ids=["worker"])]

        async def list_sessions(self, user_id, agent_id):
            return [session] if agent_id == "worker" else []

    assert await _workspace_is_referenced(Storage(), "alice", "shared")


@pytest.mark.asyncio
async def test_owned_legacy_files_move_to_oldest_session_only(tmp_path):
    legacy = tmp_path / "agent"
    legacy.mkdir()
    (legacy / "claim.md").write_text("existing", encoding="utf-8")
    old = SimpleNamespace(config=SimpleNamespace(workspace_id="old-workspace"))
    new = SimpleNamespace(config=SimpleNamespace(workspace_id="new-workspace"))

    class Storage:
        async def get_agent(self, user_id, agent_id):
            return SimpleNamespace(id=agent_id) if user_id == "alice" else None

        async def list_sessions(self, user_id, agent_id):
            return [new, old]  # production order: newest first

    manager = AccountWorkspaceManager(str(tmp_path))
    manager.bind_storage(Storage())
    new_workspace = await manager.get_workspace(
        "alice", "agent", "new-session", "new-workspace",
    )
    assert not (Path(new_workspace.workdir) / "claim.md").exists()
    old_workspace = await manager.get_workspace(
        "alice", "agent", "old-session", "old-workspace",
    )
    assert (Path(old_workspace.workdir) / "claim.md").read_text() == "existing"
    assert not legacy.exists()
    await manager.close_all()


@pytest.mark.asyncio
async def test_shared_agent_legacy_directory_is_never_migrated(tmp_path):
    legacy = tmp_path / "shared-agent"
    legacy.mkdir()
    (legacy / "owner-secret.md").write_text("private", encoding="utf-8")

    class Storage:
        async def get_agent(self, user_id, agent_id):
            return None

    manager = AccountWorkspaceManager(str(tmp_path))
    manager.bind_storage(Storage())
    workspace = await manager.get_workspace(
        "viewer", "shared-agent", "session", "viewer-workspace",
    )
    assert not (Path(workspace.workdir) / "owner-secret.md").exists()
    assert legacy.exists()
    await manager.close_all()
