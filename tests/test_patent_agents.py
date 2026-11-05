"""专利写作五智能体：模板约束与默认检索工具装配。"""

import asyncio
import json
from dataclasses import dataclass

import pytest

from app import patent_search_tools as pst
from app.patent_quality_tools import (
    CheckClaimSupport,
    CheckPatentTerminology,
    ValidateClaimStructure,
)
from app.prompt_templates import list_prompt_templates


@dataclass
class _AgentData:
    system_prompt: str


@dataclass
class _Record:
    data: _AgentData


class _Storage:
    def __init__(self, prompt: str) -> None:
        self.record = _Record(_AgentData(prompt))

    async def get_agent(self, user_id: str, agent_id: str):
        return self.record


def test_three_patent_agent_templates_have_search_contract():
    templates = {
        item["name"]: item
        for item in list_prompt_templates()
        if item["name"].startswith("专利")
    }
    drafting = {
        "专利权利要求智能体",
        "专利背景与发明内容智能体",
        "专利具体实施方式智能体",
    }
    expected = drafting | {
        "专利技术信息提取智能体",
        "专利技术方案分析智能体",
    }
    assert expected <= templates.keys()
    for name in drafting:
        content = templates[name]["content"]
        assert pst.PATENT_AGENT_PROFILE_MARKER in content
        assert "必须" in content
        assert "search_patents" in content
        assert "get_patent_details" in content
        assert "不得" in content and "不重复" in content
    assert pst.PATENT_EXTRACTION_PROFILE_MARKER in templates["专利技术信息提取智能体"]["content"]
    assert pst.PATENT_ANALYSIS_PROFILE_MARKER in templates["专利技术方案分析智能体"]["content"]


def test_new_patent_agents_expose_input_and_json_output_contracts():
    templates = {item["name"]: item["content"] for item in list_prompt_templates()}
    extraction = templates["专利技术信息提取智能体"]
    assert all(field in extraction for field in ("content", "pubid", "tech_info"))
    assert '"技术背景"' in extraction and "流式" in extraction
    analysis = templates["专利技术方案分析智能体"]
    assert all(field in analysis for field in ("problem", "cur_solution_arr", "key_solution_arr"))
    assert '"problem_relations"' in analysis and "search_journals" in analysis


@pytest.mark.asyncio
async def test_profile_agent_gets_patent_search_and_quality_tools():
    tools = await pst.patent_search_tools_for_agent(
        _Storage(pst.PATENT_AGENT_PROFILE_MARKER), "admin", "agent-1",
    )
    assert [tool.name for tool in tools] == [
        "search_patents",
        "get_patent_details",
        "search_journals",
        "validate_claim_structure",
        "check_patent_terminology",
        "check_claim_support",
    ]
    assert all(tool.is_read_only for tool in tools)


@pytest.mark.asyncio
async def test_regular_agent_does_not_get_patent_search_tools():
    tools = await pst.patent_search_tools_for_agent(
        _Storage("普通助手"), "admin", "agent-2",
    )
    assert tools == []


@pytest.mark.asyncio
async def test_extraction_agent_stays_fast_without_search_tools():
    tools = await pst.patent_search_tools_for_agent(
        _Storage(pst.PATENT_EXTRACTION_PROFILE_MARKER), "admin", "agent-2",
    )
    assert tools == []


@pytest.mark.asyncio
async def test_analysis_agent_gets_extensible_patent_and_web_search():
    tools = await pst.patent_search_tools_for_agent(
        _Storage(pst.PATENT_ANALYSIS_PROFILE_MARKER), "admin", "agent-3",
    )
    assert [tool.name for tool in tools] == [
        "search_patents", "get_patent_details", "search_journals",
        "validate_claim_structure", "check_patent_terminology",
        "check_claim_support", "anysearch_search",
    ]


@pytest.mark.asyncio
async def test_registry_tool_reuses_existing_backend_router(monkeypatch):
    captured = {}

    async def fake_call_backend(spec, arguments):
        captured.update({"name": spec["name"], "arguments": arguments})
        return {"data": [{"pubid": "CN-test"}]}

    monkeypatch.setattr(pst, "call_backend", fake_call_backend)
    spec = pst._tool_specs()["search_patents"]
    tool = pst.PatentRegistrySearchTool(spec, {})
    chunk = await tool.call(mode="natural", query="教育游戏", size=10)

    assert captured == {
        "name": "search_patents",
        "arguments": {"mode": "natural", "query": "教育游戏", "size": 10},
    }
    assert json.loads(chunk.content[0].text)["data"][0]["pubid"] == "CN-test"


@pytest.mark.asyncio
async def test_details_failure_uses_search_evidence_from_same_session(monkeypatch):
    detail_calls = 0

    async def fake_call_backend(spec, arguments):
        nonlocal detail_calls
        if spec["name"] == "search_patents":
            return {
                "data": [{
                    "pubid": "CN112131387A",
                    "title": "教育游戏",
                    "abst": "检索摘要",
                    "desc_detail": "说明书原文",
                }],
            }
        detail_calls += 1
        return {"data": {"patents": [], "err_msg": "访问数据库异常"}}

    monkeypatch.setattr(pst, "call_backend", fake_call_backend)
    cache = {}
    specs = pst._tool_specs()
    search = pst.PatentRegistrySearchTool(specs["search_patents"], cache)
    details = pst.PatentRegistrySearchTool(specs["get_patent_details"], cache)

    await search.call(mode="natural", query="教育游戏", size=10)
    chunk = await details.call(
        pubids=["CN112131387A"],
        patent_source="cn",
    )
    result = json.loads(chunk.content[0].text)

    assert result["evidence_level"] == "search_result_fallback"
    assert result["data"]["patents"][0]["desc_detail"] == "说明书原文"
    assert "访问数据库异常" in result["warning"]

    second = await details.call(
        pubids=["CN112131387A"],
        patent_source="cn",
    )
    second_result = json.loads(second.content[0].text)
    assert detail_calls == 1
    assert second_result["circuit_open"] is True
    assert "停止继续请求 30020" in second_result["warning"]


@pytest.mark.asyncio
async def test_concurrent_detail_calls_stop_after_first_database_failure(monkeypatch):
    calls = 0

    async def fake_call_backend(spec, arguments):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"data": {"patents": [], "err_msg": "访问数据库异常"}}

    monkeypatch.setattr(pst, "call_backend", fake_call_backend)
    details = pst.PatentRegistrySearchTool(
        pst._tool_specs()["get_patent_details"],
        {},
    )
    results = await asyncio.gather(
        details.call(pubids=["CN1A"], patent_source="cn"),
        details.call(pubids=["CN2A"], patent_source="cn"),
    )

    assert calls == 1
    assert all(
        json.loads(result.content[0].text)["circuit_open"] is True
        for result in results
    )


@pytest.mark.asyncio
async def test_details_reuses_cached_patents_and_only_fetches_missing_ids(monkeypatch):
    calls = []

    async def fake_call_backend(spec, arguments):
        calls.append(list(arguments["pubids"]))
        return {
            "data": {
                "patents": [
                    {
                        "pubid": pubid,
                        "title": f"title-{pubid}",
                        "abst": f"abstract-{pubid}",
                        "claimsIndList": [],
                        "desc_detail": f"detail-{pubid}",
                    }
                    for pubid in arguments["pubids"]
                ],
            },
        }

    monkeypatch.setattr(pst, "call_backend", fake_call_backend)
    details = pst.PatentRegistrySearchTool(
        pst._tool_specs()["get_patent_details"],
        {},
    )

    await details.call(pubids=["CN1", "CN2"], patent_source="cn")
    chunk = await details.call(pubids=["CN2", "CN3"], patent_source="cn")
    result = json.loads(chunk.content[0].text)

    assert calls == [["CN1", "CN2"], ["CN3"]]
    assert [item["pubid"] for item in result["data"]["patents"]] == ["CN2", "CN3"]


@pytest.mark.asyncio
async def test_claim_structure_reports_broken_references_and_missing_number():
    chunk = await ValidateClaimStructure().call(claims="""\
1. 一种检测系统，包括传感器。
3. 根据权利要求4所述的检测系统，其特征在于还包括控制器。
4. 根据权利要求9所述的检测系统，其特征在于还包括显示器。
""")
    result = json.loads(chunk.content[0].text)
    assert result["passed"] is False
    assert {issue["code"] for issue in result["issues"]} == {
        "MISSING_NUMBERS",
        "SELF_OR_FORWARD_REFERENCE",
        "REFERENCE_NOT_FOUND",
    }


@pytest.mark.asyncio
async def test_claim_structure_accepts_independent_and_dependent_claims():
    chunk = await ValidateClaimStructure().call(claims="""\
1. 一种检测系统，包括传感器和控制器。
2. 根据权利要求1所述的检测系统，其特征在于，所述传感器为温度传感器。
3. 一种使用权利要求1所述检测系统的检测方法。
""")
    result = json.loads(chunk.content[0].text)
    assert result["passed"] is True
    assert result["claim_numbers"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_terminology_check_reports_alias_and_missing_section():
    chunk = await CheckPatentTerminology().call(
        documents={"权利要求": "温度传感器采集数据", "实施方式": "探头采集数据"},
        terminology=[{"canonical": "温度传感器", "aliases": ["探头"]}],
    )
    result = json.loads(chunk.content[0].text)
    assert result["passed"] is False
    assert {issue["code"] for issue in result["issues"]} == {
        "CANONICAL_TERM_MISSING",
        "ALIAS_USED",
    }


@pytest.mark.asyncio
async def test_claim_support_builds_evidence_matrix_for_each_section():
    chunk = await CheckClaimSupport().call(
        features=[
            {"claim": 1, "feature": "温度传感器"},
            {"claim": 2, "feature": "无线通信模块", "alternatives": ["无线收发器"]},
        ],
        documents={
            "发明内容": "系统包括温度传感器和无线通信模块。",
            "具体实施方式": "温度传感器连接无线收发器。",
        },
    )
    result = json.loads(chunk.content[0].text)
    assert result["passed"] is True
    assert result["coverage_matrix"][1]["hits"]["具体实施方式"] == ["无线收发器"]


@pytest.mark.asyncio
async def test_claim_support_reports_uncovered_feature():
    chunk = await CheckClaimSupport().call(
        features=[{"claim": 2, "feature": "校准模块"}],
        documents={"发明内容": "包括传感器。", "具体实施方式": "传感器采样。"},
    )
    result = json.loads(chunk.content[0].text)
    assert result["passed"] is False
    assert result["issues"][0]["sections"] == ["发明内容", "具体实施方式"]
