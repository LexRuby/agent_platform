"""Account project registry tests."""

import json

import pytest

from app.workspace_projects import WorkspaceProjectStore


def test_projects_are_account_scoped_and_persisted(tmp_path):
    path = tmp_path / "projects.json"
    store = WorkspaceProjectStore(path)
    project = store.create("alice", "专利项目")

    assert project.workspace_id.startswith("project_")
    assert [item.name for item in store.list("alice")] == ["专利项目"]
    assert store.list("bob") == []
    assert WorkspaceProjectStore(path).list("alice")[0] == project


def test_project_name_is_normalized_and_unique_per_account(tmp_path):
    store = WorkspaceProjectStore(tmp_path / "projects.json")
    assert store.create("alice", "  专利   项目  ").name == "专利 项目"
    with pytest.raises(ValueError, match="同名项目"):
        store.create("alice", "专利 项目")
    assert store.create("bob", "专利 项目").name == "专利 项目"


def test_project_registry_writes_valid_json_atomically(tmp_path):
    path = tmp_path / "projects.json"
    WorkspaceProjectStore(path).create("alice", "Claims")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["alice"][0]["name"] == "Claims"
    assert not path.with_suffix(".tmp").exists()
