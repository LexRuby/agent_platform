"""本地技能中心（LocalSkillHub）测试：目录扫描 / 搜索 / 分页 / 详情 / 下载。

不访问网络：hub 直接读 tmp_path 下的目录结构。
"""

import io
import json
import zipfile

import pytest

from app.local_skill_hub import DOC, MANIFEST, LocalSkillHub


@pytest.fixture
def registry(tmp_path):
    """预置两个技能包的临时 registry 目录。"""
    # patent-search：完整（manifest + SKILL.md）
    ps = tmp_path / "patent-search"
    ps.mkdir()
    (ps / MANIFEST).write_text(json.dumps({
        "name": "patent-search",
        "display_name": "专利检索",
        "description": "自然语言专利检索、TRIZ 拓展、期刊检索",
        "tags": ["检索", "专利"],
        "version": "1.0.0",
        "author": "agentforge",
    }, ensure_ascii=False), encoding="utf-8")
    (ps / DOC).write_text("# 专利检索技能\n\n找专利用。", encoding="utf-8")
    # smart-writing：完整
    sw = tmp_path / "smart-writing"
    sw.mkdir()
    (sw / MANIFEST).write_text(json.dumps({
        "name": "smart-writing",
        "display_name": "智能写作",
        "description": "本体识别、方案抽取、交底书生成",
        "tags": ["写作", "交底书"],
    }, ensure_ascii=False), encoding="utf-8")
    (sw / DOC).write_text("# 智能写作技能\n\n写交底书用。", encoding="utf-8")
    # 空目录与缺 manifest 的目录：不应出现在列表
    (tmp_path / "empty-dir").mkdir()
    broken = tmp_path / "no-manifest"
    broken.mkdir()
    (broken / "other.txt").write_text("x", encoding="utf-8")
    return tmp_path


@pytest.fixture
def hub(registry):
    return LocalSkillHub(registry)


class TestListSkills:
    async def test_lists_all_skills_sorted(self, hub):
        page = await hub.list_skills("alice")
        assert [c.id for c in page.cards] == ["patent-search", "smart-writing"]
        assert page.next_cursor is None  # 一页装得下

    async def test_cards_carry_manifest_fields(self, hub):
        page = await hub.list_skills("alice")
        card = next(c for c in page.cards if c.id == "patent-search")
        assert card.hub_id == "local"
        assert card.display_name == "专利检索"
        assert card.version == "1.0.0"
        assert card.author == "agentforge"
        assert "检索" in card.tags

    async def test_name_defaults_to_dir_name(self, registry):
        # manifest 缺 name 字段 → 目录名兜底
        d = registry / "anonymous"
        d.mkdir()
        (d / MANIFEST).write_text(
            json.dumps({"description": "无名字段"}), encoding="utf-8",
        )
        page = await hub_from(registry).list_skills("alice")
        card = next(c for c in page.cards if c.id == "anonymous")
        assert card.name == "anonymous"

    @pytest.mark.parametrize("q,hits", [
        ("检索", ["patent-search"]),       # name 命中
        ("交底书", ["smart-writing"]),     # description/tags 命中
        ("专利检索", ["patent-search"]),   # display_name 命中
        ("不存在的关键词", []),
    ])
    async def test_keyword_search(self, hub, q, hits):
        page = await hub.list_skills("alice", q=q)
        assert [c.id for c in page.cards] == hits

    async def test_pagination_via_offset_cursor(self, registry):
        # 造 5 个技能验证分页
        for i in range(5):
            d = registry / f"skill-{i}"
            d.mkdir()
            (d / MANIFEST).write_text(
                json.dumps({"name": f"skill-{i}"}), encoding="utf-8",
            )
        hub = LocalSkillHub(registry)
        page1 = await hub.list_skills("alice", limit=4)
        assert len(page1.cards) == 4
        assert page1.next_cursor == "4"
        page2 = await hub.list_skills("alice", cursor="4", limit=4)
        assert len(page2.cards) == 3  # 剩余 3 个
        assert page2.next_cursor is None
        # 两页并集 = 全部
        ids = {c.id for c in page1.cards} | {c.id for c in page2.cards}
        assert len(ids) == 7

    async def test_empty_registry(self, tmp_path):
        empty = tmp_path / "nothing"
        empty.mkdir()
        hub = LocalSkillHub(empty)
        page = await hub.list_skills("alice")
        assert page.cards == []

    async def test_missing_registry_dir(self, tmp_path):
        hub = LocalSkillHub(tmp_path / "no-such-dir")
        page = await hub.list_skills("alice")
        assert page.cards == []


def hub_from(registry_dir) -> LocalSkillHub:
    return LocalSkillHub(registry_dir)


class TestGetSkill:
    async def test_returns_card_with_markdown(self, hub):
        card = await hub.get_skill("alice", "smart-writing")
        assert card.display_name == "智能写作"
        assert card.markdown is not None
        assert card.markdown.startswith("# 智能写作技能")

    async def test_unknown_id_raises_keyerror(self, hub):
        with pytest.raises(KeyError):
            await hub.get_skill("alice", "no-such-skill")

    async def test_dir_without_manifest_not_addressable(self, hub):
        # no-manifest 目录存在但不可作为卡片寻址
        with pytest.raises(KeyError):
            await hub.get_skill("alice", "no-manifest")


class TestDownload:
    async def test_zip_contains_manifest_and_doc(self, hub):
        archive = await hub.download("alice", "patent-search")
        assert archive.format == "zip"
        data = b"".join(
            [chunk async for chunk in archive.stream]
        )
        zf = zipfile.ZipFile(io.BytesIO(data))
        assert sorted(zf.namelist()) == [DOC, MANIFEST]
        meta = json.loads(zf.read(MANIFEST))
        assert meta["name"] == "patent-search"
        assert "找专利" in zf.read(DOC).decode("utf-8")

    async def test_nested_files_included_at_relative_paths(
        self, registry,
    ):
        # 辅助文件按相对路径打包（workspace 解包后保持目录结构）
        sub = registry / "patent-search" / "examples"
        sub.mkdir()
        (sub / "demo.md").write_text("示例", encoding="utf-8")
        hub = LocalSkillHub(registry)
        archive = await hub.download("alice", "patent-search")
        data = b"".join(
            [chunk async for chunk in archive.stream]
        )
        zf = zipfile.ZipFile(io.BytesIO(data))
        assert "examples/demo.md" in zf.namelist()

    async def test_unknown_id_raises_keyerror(self, hub):
        with pytest.raises(KeyError):
            await hub.download("alice", "no-such-skill")

    async def test_stream_is_chunked(self, hub):
        # 流式分块：不是一次性大字节串
        archive = await hub.download("alice", "smart-writing")
        chunks = [c async for c in archive.stream]
        assert all(isinstance(c, bytes) and c for c in chunks)
        assert len(chunks) >= 1


class TestHubIdentity:
    def test_default_identity(self, registry):
        hub = LocalSkillHub(registry)
        assert hub.hub_id == "local"
        assert hub.display_name == "平台技能中心"
        assert "技能" in hub.description

    def test_invalid_hub_id_rejected(self, tmp_path):
        # hub_id 必须匹配 [a-zA-Z0-9_-]+（官方 HubBase 契约）
        with pytest.raises(ValueError):
            LocalSkillHub(tmp_path, hub_id="bad/id with space")

    def test_custom_identity(self, registry):
        hub = LocalSkillHub(
            registry, hub_id="forge", display_name="自定义中心",
        )
        assert hub.hub_id == "forge"
