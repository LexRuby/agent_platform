"""本地技能中心（Skill Hub）：从 skill_registry/ 目录提供技能包。

背景（2026-09-03 用户反馈）：技能中心页空空如也，但平台明明已有
专利检索 / 智能写作两大能力（MCP :30110 / :30111）。官方 skill 体系
只带 ClawHub 一个在线 hub（外网注册表，不适用内网部署），本模块补一个
零依赖的本地 hub：

- 技能源：``skill_registry/<技能名>/`` 目录，含 ``skill.json``（卡片
  元数据，字段同官方 SkillCard）+ ``SKILL.md``（技能说明，教 Agent
  怎么用）+ 任意辅助文件
- 挂进 ``create_app(skill_hubs=[...])`` 后，官方技能中心页 / 安装 /
  会话工作区装配全部自动可用，前端零改动
- 新增技能 = 加一个目录，无需改代码

download 按官方 SkillArchive 契约流式返回 zip（SKILL.md 位于压缩包
根，workspace_service.install_skill 直接解包）。
"""

import asyncio
import io
import json
import logging
import zipfile
from pathlib import Path
from typing import AsyncIterator

from agentscope.app.hub import SkillHubBase
from agentscope.app.hub._skill._base import SkillArchive
from agentscope.app.hub._skill._card import SkillCard, SkillHubPage

_logger = logging.getLogger("agentforge.skill_hub")

MANIFEST = "skill.json"
DOC = "SKILL.md"


class LocalSkillHub(SkillHubBase):
    """从本地目录提供技能包的 hub。

    目录结构::

        skill_registry/
          patent-search/
            skill.json    # {"name": ..., "description": ..., "tags": [...]}
            SKILL.md      # 技能说明（markdown）
          smart-writing/
            ...
    """

    def __init__(
        self,
        base_dir: str | Path = "skill_registry",
        hub_id: str = "local",
        display_name: str = "平台技能中心",
        description: str = "本平台内置的技能包（专利检索、智能写作等）",
    ) -> None:
        super().__init__(
            hub_id=hub_id,
            display_name=display_name,
            description=description,
        )
        self._dir = Path(base_dir)

    def _skill_dir(self, card_id: str) -> Path | None:
        """卡片 id → 技能目录（id 即目录名；不存在返回 None）。"""
        p = self._dir / card_id
        if not p.is_dir() or not (p / MANIFEST).exists():
            return None
        return p

    def _cards(self) -> list[Path]:
        """全部技能目录，按名称排序（目录序即浏览序）。"""
        if not self._dir.is_dir():
            return []
        return sorted(
            p for p in self._dir.iterdir()
            if p.is_dir() and (p / MANIFEST).exists()
        )

    def _load_card(self, skill_dir: Path) -> SkillCard:
        """读 skill.json 构造卡片（markdown 由 get_skill 单独补）。"""
        meta = json.loads(
            (skill_dir / MANIFEST).read_text(encoding="utf-8"),
        )
        meta.setdefault("name", skill_dir.name)
        doc = skill_dir / DOC
        if doc.exists():
            meta["updated_at"] = doc.stat().st_mtime
        return SkillCard(hub_id=self.hub_id, **meta)

    async def list_skills(
        self,
        user_id: str,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SkillHubPage:
        cards = [self._load_card(p) for p in self._cards()]
        if q:
            needle = q.lower()
            cards = [
                c for c in cards
                if needle in c.name.lower()
                or needle in c.description.lower()
                or needle in (c.display_name or "").lower()
                or any(needle in t.lower() for t in c.tags)
            ]
        # cursor 即 offset：本地目录无并发写，offset 分页足够
        offset = int(cursor) if cursor and cursor.isdigit() else 0
        page = cards[offset:offset + limit]
        next_off = offset + limit
        return SkillHubPage(
            cards=page,
            next_cursor=(
                str(next_off) if next_off < len(cards) else None
            ),
        )

    async def get_skill(self, user_id: str, card_id: str) -> SkillCard:
        p = self._skill_dir(card_id)
        if p is None:
            raise KeyError(card_id)
        card = self._load_card(p)
        doc = p / DOC
        card.markdown = (
            doc.read_text(encoding="utf-8") if doc.exists() else None
        )
        return card

    async def download(
        self,
        user_id: str,
        card_id: str,
        version: str | None = None,
    ) -> SkillArchive:
        p = self._skill_dir(card_id)
        if p is None:
            raise KeyError(card_id)
        # 打成内存 zip 再分块流式吐出：技能包都是小文本（<1MB），
        # 内存聚合比流式 zipfile 简单得多且不会撑爆内存
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(p))
        buf.seek(0)

        async def stream() -> AsyncIterator[bytes]:
            while True:
                chunk = await asyncio.to_thread(buf.read, 64 * 1024)
                if not chunk:
                    break
                yield chunk

        return SkillArchive(format="zip", stream=stream())
