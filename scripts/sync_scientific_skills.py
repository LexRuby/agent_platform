#!/usr/bin/env python3
"""从 K-Dense-AI/scientific-agent-skills 同步科学技能包到 skill_registry/。

源仓库：https://github.com/K-Dense-AI/scientific-agent-skills
每个技能包 = skills/<name>/（SKILL.md + references/scripts/assets）。

本脚本做的事：
1. 解析 SKILL.md 的 YAML frontmatter（name/description/metadata.version）
2. 生成 skill.json（LocalSkillHub 卡片，技能中心页展示用）
3. 整目录复制到 skill_registry/<name>/（可重复执行，幂等覆盖）

用法：
    python scripts/sync_scientific_skills.py /path/to/scientific-agent-skills
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

AGENTFORGE_ROOT = Path(__file__).resolve().parent.parent
SKILL_REGISTRY = AGENTFORGE_ROOT / "skill_registry"

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(skill_md: Path) -> dict:
    """极简 YAML frontmatter 解析（仅取顶层标量 + metadata.version）。"""
    text = skill_md.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        # 只处理 "key: value" 顶层标量；description 可能极长但不换行（源仓库如此）
        kv = re.match(r"^([a-z-]+):\s*(.*)$", line)
        if kv:
            key, val = kv.group(1), kv.group(2).strip().strip('"').strip("'")
            out[key] = val
    # metadata.version: "1.3" 形式
    vm = re.search(r'^\s+version:\s*"?([^"\n]+)"?', m.group(1), re.MULTILINE)
    if vm:
        out["_version"] = vm.group(1).strip()
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    src_root = Path(sys.argv[1]).resolve()
    skills_dir = src_root / "skills"
    if not skills_dir.is_dir():
        print(f"错误：未找到 {skills_dir}（应为克隆的 scientific-agent-skills 仓库）")
        return 2

    SKILL_REGISTRY.mkdir(exist_ok=True)
    synced, skipped = [], []
    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            skipped.append(f"{skill_dir.name}: 无 SKILL.md")
            continue
        meta = parse_frontmatter(skill_md)
        name = meta.get("name") or skill_dir.name
        description = meta.get("description", "").strip()
        if not description:
            skipped.append(f"{skill_dir.name}: frontmatter 缺 description")
            continue

        dst = SKILL_REGISTRY / name
        # 整目录同步（幂等：先清空再复制，保证删除的文件不残留）
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(skill_dir, dst)

        # LocalSkillHub 卡片：display_name/description 技能中心页直接展示
        card = {
            "name": name,
            "display_name": name,
            "description": description,
            "tags": ["science", "scientific-agent-skills"],
            "version": meta.get("_version", "1.0.0"),
            "author": "K-Dense-AI",
        }
        (dst / "skill.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
        )
        synced.append(name)

    print(f"同步完成：{len(synced)} 个技能包 → skill_registry/")
    if skipped:
        print(f"跳过 {len(skipped)} 个：")
        for s in skipped:
            print(f"  - {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
