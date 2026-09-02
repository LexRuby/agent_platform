from agentscope.tool import ToolChunk

from app.tools.base import MidplatformTool


class WritingGenerate(MidplatformTool):
    name = "writing_generate"
    description = (
        "调用写作服务，基于给定大纲与素材数据生成结构化报告初稿（Markdown）。"
        "适用：已收集齐数据与人工确认的结论，需要产出最终报告时调用。"
        "不适用：只是润色一段文字（用 writing_polish）或列大纲（用 writing_outline）。"
    )
    # TODO: 对齐中台真实端点
    endpoint = "/writing/generate"
    is_read_only = False
    input_schema = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "报告标题",
            },
            "outline": {
                "type": "string",
                "description": "报告大纲，每节一行",
            },
            "material": {
                "type": "string",
                "description": "素材内容：数据、检索结果、人工确认结论等",
            },
            "style": {
                "type": "string",
                "description": "文风要求，如“严谨、面向家长、避免术语”",
            },
        },
        "required": ["title", "outline", "material"],
    }

    async def call(self, **kwargs) -> ToolChunk:
        data = await self.post(kwargs)
        return self.to_chunk(data)


class WritingPolish(MidplatformTool):
    name = "writing_polish"
    description = "对给定文本做润色、扩写或风格转换。适用：初稿已成，需要局部优化时调用。"
    # TODO: 对齐中台真实端点
    endpoint = "/writing/polish"
    is_read_only = False
    input_schema = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "待润色文本",
            },
            "requirement": {
                "type": "string",
                "description": "润色要求，如“更口语化”“压缩到 300 字以内”",
            },
        },
        "required": ["text"],
    }

    async def call(self, text: str, requirement: str = "") -> ToolChunk:
        data = await self.post({"text": text, "requirement": requirement})
        return self.to_chunk(data)
