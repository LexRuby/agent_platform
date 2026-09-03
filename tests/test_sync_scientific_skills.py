"""scientific-agent-skills 同步脚本测试：frontmatter 解析 / 卡片生成 / 幂等。

不访问网络：用 tmp_path 构造模拟源仓库（skills/<name>/SKILL.md + references），
直接调用脚本函数验证输出。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.sync_scientific_skills import parse_frontmatter

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sync_scientific_skills.py"


def make_src(root: Path) -> Path:
    """构造模拟 scientific-agent-skills 仓库。"""
    skills = root / "skills"
    # 正常技能包：frontmatter 含 name/description/metadata.version
    rdkit = skills / "rdkit"
    (rdkit / "references").mkdir(parents=True)
    (rdkit / "SKILL.md").write_text(
        "---\n"
        "name: rdkit\n"
        "description: Cheminformatics toolkit for molecular control.\n"
        "license: BSD-3-Clause license\n"
        "metadata:\n"
        '  version: "1.3"\n'
        "  skill-author: K-Dense Inc.\n"
        "---\n\n"
        "# RDKit Cheminformatics Toolkit\n\nUse it for molecules.\n",
        encoding="utf-8",
    )
    (rdkit / "references" / "guide.md").write_text("guide", encoding="utf-8")
    # description 含引号的技能包（边界：strip 引号但不破坏内容）
    datamol = skills / "datamol"
    datamol.mkdir(parents=True)
    (datamol / "SKILL.md").write_text(
        "---\n"
        "name: datamol\n"
        'description: "Wrapper around RDKit with simpler API."\n'
        "metadata:\n"
        '  version: "2.0"\n'
        "---\n\n# Datamol\n",
        encoding="utf-8",
    )
    # 缺 SKILL.md 的目录：跳过
    (skills / "no-doc").mkdir()
    # SKILL.md 缺 description：跳过
    broken = skills / "no-desc"
    broken.mkdir()
    (broken / "SKILL.md").write_text(
        "---\nname: no-desc\n---\n\n# x\n", encoding="utf-8"
    )
    return root


class TestParseFrontmatter:
    def test_extracts_scalars_and_version(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text(
            "---\nname: rdkit\ndescription: Mol toolkit.\n"
            'metadata:\n  version: "1.3"\n---\nbody\n',
            encoding="utf-8",
        )
        meta = parse_frontmatter(md)
        assert meta["name"] == "rdkit"
        assert meta["description"] == "Mol toolkit."
        assert meta["_version"] == "1.3"

    def test_no_frontmatter_returns_empty(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text("# plain doc\n", encoding="utf-8")
        assert parse_frontmatter(md) == {}

    def test_quoted_description_unwrapped(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text(
            '---\nname: x\ndescription: "Quoted desc."\n---\n',
            encoding="utf-8",
        )
        assert parse_frontmatter(md)["description"] == "Quoted desc."


class TestSyncScript:
    def run_script(self, src: Path, registry: Path) -> subprocess.CompletedProcess:
        env = {"PYTHONPATH": str(SCRIPT.parent.parent), "PATH": "/usr/bin:/bin"}
        import os

        env = {**os.environ, "PYTHONPATH": str(SCRIPT.parent.parent)}
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(src)],
            capture_output=True,
            text=True,
            env=env,
            cwd=registry.parent,  # AGENTFORGE_ROOT 取脚本自身路径，registry 隔离见下
        )

    def test_sync_generates_cards_and_copies(self, tmp_path, monkeypatch):
        """同步后：skill.json 卡片齐全、SKILL.md/references 复制到位。

        注意脚本写入 AGENTFORGE_ROOT/skill_registry，为隔离用 monkeypatch
        重定向模块常量后调用 main()，而不是跑子进程。
        """
        src = make_src(tmp_path / "src")
        registry = tmp_path / "registry"
        registry.mkdir()

        import scripts.sync_scientific_skills as mod

        monkeypatch.setattr(mod, "SKILL_REGISTRY", registry)
        monkeypatch.setattr(
            "sys.argv", ["sync", str(src)]
        )
        rc = mod.main()
        assert rc == 0

        # 正常包：卡片 + 文档 + references
        card = json.loads((registry / "rdkit" / "skill.json").read_text("utf-8"))
        assert card["name"] == "rdkit"
        assert card["description"] == "Cheminformatics toolkit for molecular control."
        assert card["author"] == "K-Dense-AI"
        assert card["version"] == "1.3"
        assert "science" in card["tags"]
        assert (registry / "rdkit" / "SKILL.md").is_file()
        assert (registry / "rdkit" / "references" / "guide.md").read_text() == "guide"

        # 引号 description 被剥壳
        card2 = json.loads((registry / "datamol" / "skill.json").read_text("utf-8"))
        assert card2["description"] == "Wrapper around RDKit with simpler API."
        assert card2["version"] == "2.0"

        # 坏包不进入 registry
        assert not (registry / "no-doc").exists()
        assert not (registry / "no-desc").exists()

    def test_sync_is_idempotent_and_removes_stale(self, tmp_path, monkeypatch):
        """重复同步幂等；源侧删除的文件不会残留在 registry。"""
        src = make_src(tmp_path / "src")
        registry = tmp_path / "registry"
        registry.mkdir()

        import scripts.sync_scientific_skills as mod

        monkeypatch.setattr(mod, "SKILL_REGISTRY", registry)
        monkeypatch.setattr("sys.argv", ["sync", str(src)])
        assert mod.main() == 0

        # 第二轮前在源里删除 references/guide.md
        (src / "skills" / "rdkit" / "references" / "guide.md").unlink()
        assert mod.main() == 0
        assert not (registry / "rdkit" / "references" / "guide.md").exists()
        # 其余文件仍在
        assert (registry / "rdkit" / "SKILL.md").is_file()

    def test_synced_cards_visible_to_local_skill_hub(self, tmp_path, monkeypatch):
        """同步产物能被 LocalSkillHub 扫描上架（端到端衔接）。"""
        from app.local_skill_hub import LocalSkillHub

        src = make_src(tmp_path / "src")
        registry = tmp_path / "registry"
        registry.mkdir()

        import scripts.sync_scientific_skills as mod

        monkeypatch.setattr(mod, "SKILL_REGISTRY", registry)
        monkeypatch.setattr("sys.argv", ["sync", str(src)])
        assert mod.main() == 0
        hub = LocalSkillHub(registry)
        import asyncio

        page = asyncio.run(hub.list_skills(user_id="u", q=None, limit=50))
        names = {c.id for c in page.cards}
        assert "rdkit" in names
        assert "datamol" in names
        assert "no-doc" not in names

    def test_missing_source_dir_fails(self, tmp_path, monkeypatch):
        import scripts.sync_scientific_skills as mod

        monkeypatch.setattr(mod, "SKILL_REGISTRY", tmp_path / "reg")
        monkeypatch.setattr("sys.argv", ["sync", str(tmp_path / "nowhere")])
        assert mod.main() == 2
