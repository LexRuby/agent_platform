"""直连工具层冒烟：验证 httpx 调用假中台的完整链路（认证头/重试/JSON 解析）。"""

import asyncio

from app.settings import load_settings
from app.tools.base import close_http, init_http
from app.tools.search import SearchAdmissionData, SearchKnowledge
from app.tools.writing import WritingGenerate


def _text(chunk) -> str:
    return "".join(getattr(block, "text", "") for block in chunk.content)


async def main() -> None:
    settings = load_settings()
    init_http(settings)
    try:
        cases = [
            (
                "search_admission_data",
                SearchAdmissionData(),
                {"province": "河南省", "subject_type": "物理类", "score": 621},
            ),
            ("search_knowledge", SearchKnowledge(), {"query": "计算机专业就业趋势", "top_k": 3}),
            (
                "writing_generate",
                WritingGenerate(),
                {
                    "title": "河南省高考志愿填报建议报告",
                    "outline": "冲稳保策略\n院校推荐\n风险提示",
                    "material": "华中科技大学 623 分；郑州大学 615 分",
                },
            ),
        ]
        for name, tool, kwargs in cases:
            chunk = await tool(**kwargs)
            print(f"== {name} ==")
            print(_text(chunk)[:500])
            print()
    finally:
        await close_http()


asyncio.run(main())
