"""假中台：开发期模拟内网四大能力服务；真实端点对齐后即可删除。"""

import uvicorn
from fastapi import FastAPI

app = FastAPI(title="Mock Midplatform")


@app.post("/search/query")
def search_query(payload: dict) -> dict:
    return {
        "province": payload.get("province"),
        "subject_type": payload.get("subject_type"),
        "score": payload.get("score"),
        "tiers": {
            "冲": [
                {"school": "华中科技大学", "min_score_2025": 623, "rank_2025": 9500},
                {"school": "武汉大学", "min_score_2025": 626, "rank_2025": 8200},
            ],
            "稳": [
                {"school": "郑州大学", "min_score_2025": 615, "rank_2025": 12500},
                {"school": "武汉理工大学", "min_score_2025": 612, "rank_2025": 13800},
            ],
            "保": [
                {"school": "河南大学", "min_score_2025": 598, "rank_2025": 21000},
            ],
        },
    }


@app.post("/search/knowledge")
def search_knowledge(payload: dict) -> dict:
    return {
        "query": payload.get("query"),
        "snippets": [
            {
                "doc": "2025年计算机类专业就业白皮书",
                "score": 0.92,
                "text": "计算机类专业就业率 92%，起薪中位数 8500 元/月……",
            },
            {
                "doc": "双一流院校学科评估结果",
                "score": 0.87,
                "text": "华中科技大学计算机学科评估 A……",
            },
        ],
    }


@app.post("/writing/generate")
def writing_generate(payload: dict) -> dict:
    return {
        "title": payload.get("title"),
        "markdown": (
            f"# {payload.get('title')}\n\n## 冲稳保策略\n（基于素材生成的示例正文）\n\n"
            "## 院校推荐与理由\n……\n\n## 风险提示\n数据均来自近三年录取统计。\n"
        ),
    }


@app.post("/writing/polish")
def writing_polish(payload: dict) -> dict:
    return {
        "text": f"[润色示例] {payload.get('text', '')[:200]}",
        "requirement": payload.get("requirement"),
    }


if __name__ == "__main__":
    import os

    port = int(os.environ.get("MOCK_MIDPLATFORM_PORT", "9000"))
    uvicorn.run(app, host="127.0.0.1", port=port)
