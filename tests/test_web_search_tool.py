import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.tools.web_search import IqsSearchService, build_web_search_tool


def settings(**overrides) -> Settings:
    values = {
        "model_provider": "mock",
        "web_search_provider": "iqs",
        "iqs_api_key": "test-secret",
        "iqs_base_url": "https://iqs.test",
        "iqs_engine_type": "LiteAdvanced",
        "iqs_timeout_seconds": 1,
        "iqs_max_results": 10,
        "web_search_content_chars": 100,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_iqs_search_preserves_sources_and_uses_bounded_request() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "requestId": "request-1",
                "pageItems": [
                    {
                        "title": "官方新品说明",
                        "link": "https://example.com/product",
                        "snippet": "a" * 200,
                        "rerankScore": 0.91,
                        "publishedTime": "2026-07-20T00:00:00+08:00",
                    },
                    {
                        "title": "不安全链接",
                        "link": "javascript:alert(1)",
                        "snippet": "ignored",
                        "rerankScore": 0.99,
                    },
                ],
                "searchInformation": {"searchTime": 1_250},
                "costCredits": {
                    "search": {"liteAdvancedTextSearch": 1},
                    "valueAdded": {"summary": 0, "advanced": 0},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = IqsSearchService(settings(), client=client)
        result = await service.search(
            "  降噪耳机   新品  ",
            max_results=30,
            topic="finance",
            time_range="week",
        )

    assert result["status"] == "ok"
    assert result["provider"] == "iqs"
    assert result["result_count"] == 1
    assert result["results"][0] == {
        "title": "官方新品说明",
        "url": "https://example.com/product",
        "content": "a" * 100,
        "score": 0.91,
        "published_date": "2026-07-20T00:00:00+08:00",
    }
    assert result["credits_used"] == 1.0
    assert result["response_time_seconds"] == 1.25
    assert result["request_id"] == "request-1"
    assert captured["authorization"] == "Bearer test-secret"
    assert captured["body"] == {
        "query": "降噪耳机 新品",
        "engineType": "LiteAdvanced",
        "timeRange": "OneWeek",
        "contents": {
            "mainText": False,
            "markdownText": False,
            "summary": False,
            "rerankScore": True,
        },
        "advancedParams": {"numResults": "10"},
        "category": "finance",
    }


@pytest.mark.asyncio
async def test_web_search_without_key_returns_not_configured_without_http() -> None:
    service = IqsSearchService(settings(iqs_api_key=None))
    result = await service.search("耳机趋势")
    assert result["status"] == "not_configured"
    assert result["provider"] == "iqs"
    assert result["results"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "provider_code", "code", "retryable"),
    [
        (404, "InvalidAccessKeyId.NotFound", "authentication_failed", False),
        (403, "Retrieval.NotActivate", "access_denied", False),
        (403, "Retrieval.Arrears", "usage_limit", False),
        (429, "Retrieval.Throttling.User", "rate_limited", True),
        (429, "Retrieval.TestUserQueryExceeded", "usage_limit", False),
        (500, "InternalError", "provider_error", True),
    ],
)
async def test_iqs_http_errors_are_sanitized(
    status_code: int,
    provider_code: str,
    code: str,
    retryable: bool,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"code": provider_code, "message": "provider-secret-debug-body"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = IqsSearchService(settings(), client=client)
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
        return httpx.Response(200, json={"pageItems": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tool = build_web_search_tool(IqsSearchService(settings(), client=client))
        task = asyncio.create_task(tool.ainvoke({"query": "取消测试"}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
