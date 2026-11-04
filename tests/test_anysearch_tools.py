"""AnySearch protocol and session-bound request behavior."""

import json

import httpx
import pytest

from app import anysearch_tools as web
from app import paper_writing_tools as paper


@pytest.mark.asyncio
async def test_anysearch_protocol_and_bounded_response(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "code": 0,
            "data": {
                "results": [{
                    "title": "Official benchmark",
                    "url": "https://example.org/benchmark",
                    "snippet": "Evaluation protocol",
                    "content": "must not be exposed by this thin tool",
                }],
                "metadata": {},
            },
        })

    monkeypatch.setenv("ANYSEARCH_API_KEY", "secret-test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await web.search_anysearch("benchmark protocol", 8, client=client)

    assert captured["body"] == {"query": "benchmark protocol", "max_results": 8}
    assert captured["headers"]["authorization"] == "Bearer secret-test-key"
    assert result["results"] == [{
        "title": "Official benchmark",
        "url": "https://example.org/benchmark",
        "snippet": "Evaluation protocol",
    }]


@pytest.mark.asyncio
async def test_anysearch_caches_duplicates_and_enforces_budget(monkeypatch):
    calls = 0

    async def fake_search(query, max_results):
        nonlocal calls
        calls += 1
        return {"provider": "AnySearch", "query": query, "results": []}

    monkeypatch.setattr(web, "search_anysearch", fake_search)
    tool = web.AnySearchTool()
    await tool.call(query="first")
    cached = await tool.call(query="first")
    await tool.call(query="second")
    blocked = await tool.call(query="third")

    assert calls == 2
    assert json.loads(cached.content[0].text)["cache_hit"] is True
    assert json.loads(blocked.content[0].text)["budget_exhausted"] is True


@pytest.mark.asyncio
async def test_anysearch_failure_opens_circuit(monkeypatch):
    calls = 0

    async def unavailable(query, max_results):
        nonlocal calls
        calls += 1
        raise RuntimeError("not configured")

    monkeypatch.setattr(web, "search_anysearch", unavailable)
    tool = web.AnySearchTool()
    first = await tool.call(query="first")
    second = await tool.call(query="second")

    assert calls == 1
    assert json.loads(first.content[0].text)["request_sent"] is True
    assert json.loads(second.content[0].text)["request_sent"] is False


@pytest.mark.asyncio
async def test_journal_failure_opens_circuit(monkeypatch):
    calls = 0

    async def unavailable(spec, arguments):
        nonlocal calls
        calls += 1
        raise RuntimeError("no skill returned any result")

    monkeypatch.setattr(paper, "call_backend", unavailable, raising=False)
    # Patch where the inherited implementation resolves the backend function.
    monkeypatch.setattr("app.patent_search_tools.call_backend", unavailable)
    tool = paper.BoundedJournalSearchTool(
        paper._tool_specs()["search_journals"],
        {},
    )
    first = await tool.call(query="keywords:联邦学习", size=10)
    second = await tool.call(query="keywords:隐私保护", size=10)

    assert calls == 1
    assert json.loads(first.content[0].text)["circuit_open"] is True
    assert json.loads(second.content[0].text)["request_sent"] is False


@pytest.mark.asyncio
async def test_journal_caches_duplicates_and_enforces_budget(monkeypatch):
    calls = 0

    async def successful(spec, arguments):
        nonlocal calls
        calls += 1
        return {"data": [{"title": arguments["query"]}]}

    monkeypatch.setattr("app.patent_search_tools.call_backend", successful)
    tool = paper.BoundedJournalSearchTool(
        paper._tool_specs()["search_journals"],
        {},
    )
    await tool.call(query="keywords:联邦学习", size=10)
    cached = await tool.call(query="keywords:联邦学习", size=10)
    await tool.call(query="keywords:隐私保护", size=10)
    blocked = await tool.call(query="keywords:知识蒸馏", size=10)

    assert calls == 2
    assert json.loads(cached.content[0].text)["cache_hit"] is True
    assert json.loads(blocked.content[0].text)["budget_exhausted"] is True


@pytest.mark.asyncio
async def test_journal_rejects_invalid_query_and_caps_result_count(monkeypatch):
    calls = []

    async def successful(spec, arguments):
        calls.append(arguments)
        return {"data": []}

    monkeypatch.setattr("app.patent_search_tools.call_backend", successful)
    tool = paper.BoundedJournalSearchTool(
        paper._tool_specs()["search_journals"],
        {},
    )
    invalid = await tool.call(query="federated learning", size=500)
    valid = await tool.call(
        query="keywords:联邦学习 AND keywords:隐私保护",
        size=500,
    )

    assert json.loads(invalid.content[0].text)["request_sent"] is False
    assert calls == [{
        "query": "keywords:联邦学习 AND keywords:隐私保护",
        "size": 20,
    }]
    assert json.loads(valid.content[0].text) == {"data": []}
