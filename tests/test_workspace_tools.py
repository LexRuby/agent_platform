from __future__ import annotations

from pathlib import Path

import pytest

from agentscope.permission import PermissionBehavior, PermissionContext, PermissionMode
from agentscope.tool._builtin._backend import ExecResult
from app.workspace_tools import WorkspaceLocalBackend, build_workspace_tools


def _tools(root: Path):
    return {
        tool.name: tool
        for tool in build_workspace_tools(str(root), WorkspaceLocalBackend())
    }


async def _chunks(tool, **kwargs):
    result = await tool(**kwargs)
    if hasattr(result, "__aiter__"):
        return [chunk async for chunk in result]
    return [result]


@pytest.mark.asyncio
async def test_relative_edits_and_directory_creation_are_auto_allowed(tmp_path):
    tools = _tools(tmp_path)
    context = PermissionContext(mode=PermissionMode.ACCEPT_EDITS)
    cases = [
        ("Write", {"file_path": "draft.md"}),
        ("Edit", {"file_path": "draft.md"}),
        ("Bash", {"command": "mkdir drafts"}),
        ("Bash", {"command": "touch draft.md"}),
    ]

    for name, tool_input in cases:
        decision = await tools[name].check_permissions(tool_input, context)
        assert decision.behavior is PermissionBehavior.ALLOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "tool_input"),
    [
        ("Read", {"file_path": "/etc/passwd"}),
        ("Write", {"file_path": "../outside.md"}),
        ("Glob", {"pattern": "*", "path": "../"}),
        ("Bash", {"command": "ls /"}),
        ("Bash", {"command": "cat</etc/passwd"}),
        ("Bash", {"command": "find / -name secret"}),
    ],
)
async def test_paths_outside_workspace_are_denied(tmp_path, name, tool_input):
    tool = _tools(tmp_path)[name]
    context = PermissionContext(mode=PermissionMode.ACCEPT_EDITS)

    assert await tool.check_read_only(tool_input) is False
    decision = await tool.check_permissions(tool_input, context)
    assert decision.behavior is PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_symlink_escape_is_denied(tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    tool = _tools(tmp_path)["Write"]
    context = PermissionContext(mode=PermissionMode.ACCEPT_EDITS)

    decision = await tool.check_permissions({"file_path": "link/leak.md"}, context)

    assert decision.behavior is PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_relative_write_executes_inside_workspace_and_redacts_result(tmp_path):
    tool = _tools(tmp_path)["Write"]

    chunks = await _chunks(tool, file_path="draft.md", content="hello")

    assert (tmp_path / "draft.md").read_text() == "hello"
    rendered = "\n".join(
        block.text for chunk in chunks for block in chunk.content if hasattr(block, "text")
    )
    assert str(tmp_path) not in rendered
    assert "./draft.md" in rendered
    assert str(tmp_path) not in str(chunks[-1].metadata)


@pytest.mark.asyncio
async def test_pwd_result_does_not_expose_physical_workspace(tmp_path):
    class PwdBackend(WorkspaceLocalBackend):
        async def exec_shell(self, command, *, cwd=None, timeout=None):
            assert command == ["/bin/sh", "-c", "pwd"]
            return ExecResult(exit_code=0, stdout=f"{cwd}\n".encode(), stderr=b"")

    tool = {
        item.name: item
        for item in build_workspace_tools(str(tmp_path), PwdBackend())
    }["Bash"]

    chunks = await _chunks(tool, command="pwd", description="Show workspace")
    output = "\n".join(
        block.text for chunk in chunks for block in chunk.content if hasattr(block, "text")
    )

    assert str(tmp_path) not in output
    assert output.strip() == "."


def test_tool_contract_only_mentions_relative_paths(tmp_path):
    tools = _tools(tmp_path)
    for name in ("Read", "Write", "Edit"):
        path_schema = tools[name].input_schema["properties"]["file_path"]
        assert "relative" in path_schema["description"]
        assert "absolute path" not in path_schema["description"].lower()
    for name in ("Glob", "Grep"):
        path_schema = tools[name].input_schema["properties"]["path"]
        assert "relative" in path_schema["description"]


def test_frontend_sanitizes_tool_inputs_and_outputs():
    root = Path(__file__).parents[1] / "webui-src/src/components/chat/tool-renderers"
    shared = (root / "_shared.tsx").read_text()
    default = (root / "DefaultRenderer.tsx").read_text()

    assert "sanitizeWorkspacePath(input)" in shared
    assert "sanitizeWorkspacePath(result.output)" in shared
    assert "sanitizeWorkspacePath(call.input)" in default


def test_project_space_files_are_downloadable():
    root = Path(__file__).parents[1] / "webui-src/src"
    dialog = (root / "components/dialog/WorkingDirectoryDialog.tsx").read_text()
    api = (root / "api/workspace.ts").read_text()

    assert "workspaceApi.download(agentId, sessionId, filePath)" in dialog
    assert "if (entry.is_dir) void load(entryPath)" in dialog
    assert "else void handleDownload(entryPath)" in dialog
    assert "'/workspace/files/download-token'" in api
    assert "url.searchParams.set('download', 'true')" in api
