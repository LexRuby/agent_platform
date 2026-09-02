from agentscope.tool import ToolChunk

from app.tools.base import MidplatformTool


class SearchAdmissionData(MidplatformTool):
    name = "search_admission_data"
    description = (
        "检索指定省份近三年的高校录取分数线、位次与招生计划数据。"
        "适用：用户给出省份与分数/位次，需要评估可选院校范围时调用。"
        "不适用：询问专业介绍、就业前景等非录取数据问题（这些用通用知识回答）。"
    )
    # TODO: 对齐中台真实端点
    endpoint = "/search/query"
    input_schema = {
        "type": "object",
        "properties": {
            "province": {
                "type": "string",
                "description": "省份全称，如“河南省”",
            },
            "subject_type": {
                "type": "string",
                "description": "科类，如“物理类”“历史类”",
            },
            "score": {
                "type": "integer",
                "description": "考生高考总分",
            },
            "rank": {
                "type": "integer",
                "description": "考生省内位次，未知则不传",
            },
            "year_span": {
                "type": "integer",
                "description": "回溯年数，默认 3",
            },
        },
        "required": ["province", "subject_type", "score"],
    }

    async def call(self, **kwargs) -> ToolChunk:
        payload = {k: v for k, v in kwargs.items() if v is not None}
        data = await self.post(payload)
        return self.to_chunk(data)


class SearchKnowledge(MidplatformTool):
    name = "search_knowledge"
    description = (
        "在平台知识库（院校库、专业库、就业数据、行业研报等）中做混合检索，"
        "返回带相关性分数的文档片段。适用：需要权威事实依据、且要引用出处时调用。"
    )
    # TODO: 对齐中台真实端点
    endpoint = "/search/knowledge"
    is_read_only = True
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索问题或关键词",
            },
            "top_k": {
                "type": "integer",
                "description": "返回条数，默认 5",
            },
        },
        "required": ["query"],
    }

    async def call(self, query: str, top_k: int = 5) -> ToolChunk:
        data = await self.post({"query": query, "top_k": top_k})
        return self.to_chunk(data)
