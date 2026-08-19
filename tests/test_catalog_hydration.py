from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.products.catalog.intent import ShoppingIntent
from app.products.catalog.repository import _unique_page_items
from app.products.catalog.scope import CatalogScope, ProviderRequestFingerprint
from app.products.catalog.stop_policy import CatalogStopPolicy, StopReason
from app.products.providers.base import ProviderErrorCode, ProviderSearchRequest
from app.products.providers.justone import JustOneProvider, redact
from app.products.providers.normalization import extract_items, normalize_item
from app.search.schemas import SearchFilters
from app.tools.planner import planner


def _intent(**updates) -> ShoppingIntent:
    values = {
        "category_key": " Portable-Audio ",
        "category_name": "便携音频",
        "primary_query": "降噪 耳机",
        "query_variants": ["通勤耳机", "通勤耳机", "无线耳机"],
        "platforms": ["taobao", "jingdong", "taobao"],
    }
    values.update(updates)
    return ShoppingIntent.model_validate(values)


def test_intent_normalizes_category_variants_and_platforms() -> None:
    intent = _intent()

    assert intent.category_key == "portable_audio"
    assert intent.query_variants == ["通勤耳机", "无线耳机"]
    assert intent.platforms == ["taobao", "jingdong"]


def test_catalog_settings_reject_inverted_limits_and_deadlines() -> None:
    with pytest.raises(ValueError, match="minimum <= target <= hard cap"):
        Settings(catalog_minimum_total=100, catalog_target_total=60)
    with pytest.raises(ValueError, match="soft deadline"):
        Settings(catalog_soft_deadline_seconds=61, catalog_hard_deadline_seconds=60)


def test_clarification_requires_question_and_blocks_provider() -> None:
    with pytest.raises(ValueError, match="clarification_question"):
        _intent(needs_clarification=True)

    intent = _intent(needs_clarification=True, clarification_question="预算是多少？")
    assert intent.provider_allowed is False


def test_planner_does_not_block_a_broad_search_when_category_and_budget_exist() -> None:
    result = planner.invoke(
        {
            "goal": "买500元左右的牛仔裤",
            "shopping_intent": {
                "category_key": "jeans",
                "category_name": "牛仔裤",
                "primary_query": "牛仔裤",
                "platforms": ["taobao"],
                "filters": {"min_price": 350, "max_price": 550},
                "needs_clarification": True,
                "clarification_question": "需要男款还是女款？",
            },
        }
    )

    assert result["shopping_intent"]["needs_clarification"] is False
    assert result["shopping_intent"]["clarification_question"] is None


def test_scope_hash_is_public_and_stable_across_free_text() -> None:
    first = CatalogScope.from_intent(_intent(primary_query="耳机 A"), "taobao", provider="fake")
    second = CatalogScope.from_intent(
        _intent(primary_query="完全不同的自由文本"), "taobao", provider="fake"
    )

    assert first.scope_id == second.scope_id
    assert "耳机" not in first.scope_id


def test_request_fingerprint_normalizes_query_and_changes_with_page() -> None:
    base = {
        "scope_id": "scope_1",
        "provider": "fake",
        "platform": "taobao",
        "provider_filters": SearchFilters(max_price=500),
    }
    first = ProviderRequestFingerprint(**base, normalized_query="  Noise   Cancelling ")
    equivalent = ProviderRequestFingerprint(**base, normalized_query="noise cancelling")
    next_page = ProviderRequestFingerprint(
        **base, normalized_query="noise cancelling", cursor={"page": 2}
    )

    assert first.request_key == equivalent.request_key
    assert first.request_key != next_page.request_key
    assert first.request_key != ProviderRequestFingerprint(
        **base,
        normalized_query="noise cancelling",
        request_version="provider-request-v2",
    ).request_key


def test_provider_page_items_are_deduplicated_before_database_inserts() -> None:
    first = {"item_id": "jingdong:1", "title": "first"}
    duplicate = {"item_id": "jingdong:1", "title": "duplicate"}
    second = {"item_id": "jingdong:2", "title": "second"}

    assert _unique_page_items([first, duplicate, second]) == [first, second]


def test_stop_policy_requires_all_platforms_to_start_before_target_stop() -> None:
    policy = CatalogStopPolicy(("taobao", "jingdong", "douyin"))
    policy.observe("taobao", 100, success=True, has_more=True)
    assert policy.reason() is None

    policy.observe("jingdong", 0, success=True, has_more=True)
    policy.observe("douyin", 0, success=True, has_more=True)
    assert policy.reason() == StopReason.TARGET_REACHED


def test_stop_policy_hard_cap_and_soft_deadline() -> None:
    hard_cap = CatalogStopPolicy(("taobao",), counts={"taobao": 119})
    hard_cap.observe("taobao", 1, success=True, has_more=True)
    assert hard_cap.reason() == StopReason.HARD_CAP_REACHED

    partial = CatalogStopPolicy(
        ("taobao", "jingdong"), counts={"taobao": 30, "jingdong": 30}
    )
    assert partial.reason(soft_deadline=True) == StopReason.MINIMUM_AT_DEADLINE


def test_normalizer_rejects_incomplete_rows_and_converts_douyin_cents() -> None:
    assert normalize_item("taobao", {"itemId": "1", "title": "missing price"}) is None

    item = normalize_item(
        "douyin",
        {
            "productId": 7,
            "productName": "运动相机",
            "salePrice": 129900,
            "detailUrl": "https://example.test/7",
            "soldCount": "1,234",
        },
    )
    assert item is not None
    assert item["item_id"] == "douyin:7"
    assert item["price"] == 1299.0
    assert item["sales"] == 1234


@pytest.mark.parametrize(
    ("platform", "fixture_name", "expected"),
    [
        (
            "taobao",
            "taobao_search.json",
            {
                "item_id": "taobao:10001",
                "price": 299.0,
                "sales": 12000,
                "product_url": "https://item.taobao.com/item.htm?id=10001",
            },
        ),
        (
            "jingdong",
            "jd_search.json",
            {
                "item_id": "jingdong:20001",
                "price": 199.5,
                "sales": 8000,
                "product_url": "https://item.jd.com/20001.html",
            },
        ),
        (
            "douyin",
            "douyin_search.json",
            {
                "item_id": "douyin:30001",
                "price": 159.0,
                "sales": 5000,
                "product_url": (
                    "https://haohuo.jinritemai.com/ecommerce/trade/detail/"
                    "index.html?id=30001"
                ),
            },
        ),
    ],
)
def test_verified_justone_fixtures_normalize_to_runtime_candidates(
    platform: str, fixture_name: str, expected: dict
) -> None:
    fixture = Path("datasets/justone_headphones/fixtures") / fixture_name
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    items, metadata = extract_items(payload, platform)
    normalized = normalize_item(platform, items[0])

    assert normalized is not None
    for key, value in expected.items():
        assert normalized[key] == value
    assert normalized["title"]
    assert normalized["image_url"]
    if platform == "douyin":
        assert metadata["search_id"] == "search-next-page"
        assert metadata["has_more"] is True


@pytest.mark.asyncio
async def test_douyin_provider_passes_successful_search_id_without_leaking_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [{"productId": 1, "productName": "相机", "salePrice": 100}],
                    "searchId": "next-search-id",
                    "totalPage": 2,
                },
            },
        )

    client = httpx.AsyncClient(
        base_url="https://provider.invalid", transport=httpx.MockTransport(handler)
    )
    provider = JustOneProvider(
        Settings(product_provider="justone", justone_token="private-token"), client=client
    )
    first = await provider.search(
        ProviderSearchRequest(
            provider="justone",
            platform="douyin",
            keyword="相机",
            request_key="a" * 64,
        )
    )
    await provider.search(
        ProviderSearchRequest(
            provider="justone",
            platform="douyin",
            keyword="相机",
            cursor=first.next_cursor,
            request_key="b" * 64,
        )
    )
    await client.aclose()

    assert first.status == ProviderErrorCode.OK
    assert first.next_cursor is not None
    assert first.next_cursor.search_id == "next-search-id"
    assert "page" not in requests[0].url.params
    assert requests[1].url.params["page"] == "2"
    assert requests[1].url.params["searchId"] == "next-search-id"
    assert redact({"headers": {"authorization": "secret"}, "token": "secret"}) == {
        "headers": {"authorization": "***"},
        "token": "***",
    }
