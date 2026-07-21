import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.tools.web_search import TavilySearchService, build_web_search_tool


def settings(**overrides) -> Settings:
    values = {
        "model_provider": "mock",
        "web_search_provider": "tavily",
        "tavily_api_key": "test-secret",
        "tavily_base_url": "https://api.tavily.test",
        "tavily_project_id": "globuy-tests",
        "tavily_search_depth": "basic",
        "tavily_timeout_seconds": 1,
        "web_search_content_chars": 100,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_tavily_search_preserves_sources_and_uses_bounded_basic_request() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["project"] = request.headers.get("X-Project-ID")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "query": "降噪耳机 新品",
                "results": [
                    {
                        "title": "官方新品说明",
                        "url": "https://example.com/product",
                        "content": "a" * 200,
                        "score": 0.91,
                        "published_date": "2026-07-20",
                    },
                    {
                        "title": "不安全链接",
                        "url": "javascript:alert(1)",
                        "content": "ignored",
                        "score": 0.99,
                    },
                ],
                "response_time": "1.25",
                "request_id": "request-1",
                "usage": {"credits": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = TavilySearchService(settings(), client=client)
        result = await service.search(
            "  降噪耳机   新品  ",
            max_results=30,
            topic="news",
            time_range="week",
            include_domains=["Example.com", "bad/path", "example.com"],
        )

    assert result["status"] == "ok"
    assert result["result_count"] == 1
    assert result["results"][0] == {
        "title": "官方新品说明",
        "url": "https://example.com/product",
        "content": "a" * 100,
        "score": 0.91,
        "published_date": "2026-07-20",
    }
    assert result["credits_used"] == 1.0
    assert result["request_id"] == "request-1"
    assert captured["authorization"] == "Bearer test-secret"
    assert captured["project"] == "globuy-tests"
    assert captured["body"] == {
        "query": "降噪耳机 新品",
        "search_depth": "basic",
        "topic": "news",
        "max_results": 10,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
        "auto_parameters": False,
        "time_range": "week",
        "include_domains": ["example.com"],
    }


@pytest.mark.asyncio
async def test_web_search_without_key_returns_not_configured_without_http() -> None:
    service = TavilySearchService(settings(tavily_api_key=None))
    result = await service.search("耳机趋势")
    assert result["status"] == "not_configured"
    assert result["results"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "code", "retryable"),
    [
        (401, "authentication_failed", False),
        (429, "rate_limited", True),
        (432, "usage_limit", False),
        (500, "provider_error", True),
    ],
)
async def test_tavily_http_errors_are_sanitized(
    status_code: int, code: str, retryable: bool
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="provider-secret-debug-body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = TavilySearchService(settings(), client=client)
        result = await service.search("耳机")
    assert result["status"] == "error"
    assert result["error"]["code"] == code
    assert result["error"]["retryable"] is retryable
    assert "provider-secret-debug-body" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_web_search_tool_is_async_and_propagates_cancellation() -> None:
    started = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(30)
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tool = build_web_search_tool(TavilySearchService(settings(), client=client))
        task = asyncio.create_task(tool.ainvoke({"query": "取消测试"}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
