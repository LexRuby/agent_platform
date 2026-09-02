"""中台工具的 MCP Server 封装：把内网四大能力暴露为标准 MCP（Streamable HTTP）。

供 AgentScope agent-service 的工作区默认接入，也可被任何 MCP 客户端注册。
端点: http://127.0.0.1:9200/mcp/
上游: MIDPLATFORM_BASE_URL（假中台 scripts/mock_midplatform.py 或真实中台）
"""

import os

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

MIDPLATFORM_BASE_URL = os.environ.get("MIDPLATFORM_BASE_URL", "http://127.0.0.1:9100")
MIDPLATFORM_TOKEN = os.environ.get("MIDPLATFORM_TOKEN", "")

mcp = FastMCP("midplatform", host="0.0.0.0", port=9200)


async def _call(endpoint: str, payload: dict) -> str:
    import json

    headers = {}
    if MIDPLATFORM_TOKEN:
        headers["Authorization"] = f"Bearer {MIDPLATFORM_TOKEN}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{MIDPLATFORM_BASE_URL}{endpoint}", json=payload, headers=headers,
        )
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


@mcp.tool()
async def search_admission_data(
    province: str,
    subject_type: str,
    score: int,
    rank: int | None = None,
) -> str:
    """检索指定省份近三年的高校录取分数线、位次与招生计划数据。

    适用：用户给出省份与分数/位次，需要评估可选院校范围时调用。
    不适用：询问专业介绍、就业前景等非录取数据问题。
    """
    payload = {"province": province, "subject_type": subject_type, "score": score}
    if rank is not None:
        payload["rank"] = rank
    return await _call("/search/query", payload)


@mcp.tool()
async def search_knowledge(query: str, top_k: int = 5) -> str:
    """在平台知识库（院校库、专业库、就业数据、行业研报等）中做混合检索，返回带相关性分数的文档片段。

    适用：需要权威事实依据、且要引用出处时调用。
    """
    return await _call("/search/knowledge", {"query": query, "top_k": top_k})


@mcp.tool()
async def writing_generate(title: str, outline: str, material: str) -> str:
    """根据大纲和素材生成结构化报告/文档（Markdown）。"""
    return await _call(
        "/writing/generate", {"title": title, "outline": outline, "material": material},
    )


@mcp.tool()
async def writing_polish(text: str, requirement: str = "") -> str:
    """对给定文本进行润色优化，可指定润色要求（风格/受众/语气）。"""
    return await _call(
        "/writing/polish", {"text": text, "requirement": requirement},
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
