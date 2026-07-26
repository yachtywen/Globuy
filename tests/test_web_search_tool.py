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
async def test_product_reviews_are_limited_to_top_three_xiaohongshu_and_zhihu() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Sony WH-1000XM6 小红书体验",
                        "url": "https://www.xiaohongshu.com/explore/1",
                        "content": "佩戴体验与降噪表现",
                        "score": 0.95,
                    },
                    {
                        "title": "WH-1000XM6 长期使用回答",
                        "url": "https://www.zhihu.com/question/1/answer/2",
                        "content": "长期使用后的优缺点",
                        "score": 0.93,
                    },
                    {
                        "title": "非目标网站",
                        "url": "https://example.com/review",
                        "content": "不得进入结果",
                        "score": 0.99,
                    },
                    {
                        "title": "WH-1000XM6 重复链接",
                        "url": "https://www.zhihu.com/question/1/answer/2",
                        "content": "不得重复",
                        "score": 0.92,
                    },
                    {
                        "title": "索尼 1000XM6 型号对比",
                        "url": "https://zhuanlan.zhihu.com/p/3",
                        "content": "型号对比",
                        "score": 0.9,
                    },
                    {
                        "title": "Sony WH-1000XM6 第四条目标结果",
                        "url": "https://www.xiaohongshu.com/explore/4",
                        "content": "不应超过三条",
                        "score": 0.88,
                    },
                ],
                "request_id": "review-request",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = TavilySearchService(settings(), client=client)
        result = await service.search_product_reviews("Sony WH-1000XM6")

    assert captured["body"]["query"] == '"Sony WH-1000XM6" 测评 评价 使用体验 优缺点'
    assert captured["body"]["include_domains"] == ["xiaohongshu.com", "zhihu.com"]
    assert captured["body"]["max_results"] == 10
    assert result["status"] == "complete"
    assert result["terminal"] is True
    assert result["source_kind"] == "content_review"
    assert [item["rank"] for item in result["review_results"]] == [1, 2, 3]
    assert [item["source"] for item in result["review_results"]] == [
        "小红书",
        "知乎",
        "知乎",
    ]
    assert all(item["relevance_evidence"] for item in result["review_results"])
    assert "example.com" not in result["final_text"]
    assert "第四条目标结果" not in result["final_text"]


@pytest.mark.asyncio
async def test_product_reviews_report_incomplete_instead_of_filling_other_domains() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Sony WH-1000XM6 唯一知乎结果",
                        "url": "https://zhihu.com/question/1",
                        "content": "只有一条可核验结果",
                    },
                    {
                        "title": "其他站点结果",
                        "url": "https://example.com/2",
                        "content": "不能补齐",
                    },
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tool = build_web_search_tool(TavilySearchService(settings(), client=client))
        result = await tool.ainvoke(
            {"query": "Sony WH-1000XM6", "search_mode": "product_reviews"}
        )

    assert result["status"] == "incomplete"
    assert result["result_count"] == 1
    assert result["review_results"][0]["source"] == "知乎"
    assert result["unresolved"] == ["仅检索到 1 条目标平台有效结果"]


@pytest.mark.asyncio
async def test_product_reviews_discard_target_domain_pages_unrelated_to_product() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "暑期旅行好物分享",
                        "url": "https://www.xiaohongshu.com/explore/unrelated",
                        "content": "行李箱和防晒用品使用体验",
                        "score": 0.99,
                    },
                    {
                        "title": "如何选择适合自己的耳机？",
                        "url": "https://www.zhihu.com/question/unrelated",
                        "content": "泛化的耳机品类讨论，没有提及具体品牌和型号。",
                        "score": 0.98,
                    },
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = TavilySearchService(settings(), client=client)
        result = await service.search_product_reviews("Sony WH-1000XM6")

    assert result["status"] == "incomplete"
    assert result["review_results"] == []
    assert result["discarded_irrelevant_count"] == 2
    assert "为避免答非所问" in result["final_text"]


@pytest.mark.asyncio
async def test_product_reviews_accept_compact_chinese_product_phrase() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "小米 14 Ultra 深度体验",
                        "url": "https://www.zhihu.com/question/xiaomi14ultra",
                        "content": "相机和续航表现",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = TavilySearchService(settings(), client=client)
        result = await service.search_product_reviews("了解一下小米14 Ultra")

    assert result["result_count"] == 1
    assert result["review_results"][0]["title"] == "小米 14 Ultra 深度体验"


@pytest.mark.asyncio
async def test_product_reviews_without_key_are_terminal_and_not_configured() -> None:
    service = TavilySearchService(settings(tavily_api_key=None))
    result = await service.search_product_reviews("Sony WH-1000XM6")
    assert result["status"] == "not_configured"
    assert result["terminal"] is True
    assert result["review_results"] == []


@pytest.mark.asyncio
async def test_product_reviews_fall_back_to_verified_platform_aggregate_feedback() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    candidates = [
        {
            "item_id": "taobao:1",
            "platform": "taobao",
            "title": "Sony WH-1000XM6",
            "rating": 4.8,
            "product_url": "https://item.taobao.com/item.htm?id=1",
            "attributes": {"comment_count": 2000, "rating_type": "average_rating_5"},
            "data_as_of": "2026-07-20T00:00:00Z",
        },
        {
            "item_id": "douyin:2",
            "platform": "douyin",
            "title": "Sony WH-1000XM6 抖音商品",
            "rating": 96.5,
            "attributes": {"rating_type": "good_ratio_percent"},
        },
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = TavilySearchService(settings(), client=client)
        result = await service.search_product_reviews("Sony WH-1000XM6", platform_candidates=candidates)

    assert result["status"] == "incomplete"
    assert result["source_kind"] == "platform_feedback"
    assert result["review_results"] == []
    assert [item["platform"] for item in result["platform_feedback"]] == ["taobao", "douyin"]
    assert result["platform_feedback"][0]["comment_count"] == 2000
    assert result["platform_feedback"][1]["rating_scale"] == 100
    assert all(item["aggregate_only"] is True for item in result["platform_feedback"])
    assert "不是消费者评价原文" in result["final_text"]


@pytest.mark.asyncio
async def test_product_reviews_do_not_fabricate_feedback_without_verified_platform_signals() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = TavilySearchService(settings(), client=client)
        result = await service.search_product_reviews(
            "Sony WH-1000XM6",
            platform_candidates=[
                {"item_id": "taobao:1", "platform": "taobao", "title": "Sony WH-1000XM6"}
            ],
        )

    assert result["platform_feedback"] == []
    assert result["review_results"] == []
    assert "为避免答非所问" in result["final_text"]


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
