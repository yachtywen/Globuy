from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.category.cache import category_cache_key
from app.category.extractor import CategoryExtractionNotConfigured
from app.category.normalization import CategoryAliases
from app.category.reranker import rerank_cards
from app.category.schemas import (
    AttributeDist,
    Bestseller,
    CategoryCard,
    CategoryInsightOutput,
    InsightExtractionPayload,
    PriceTier,
)
from app.category.service import CategorySearchService
from app.config import Settings
from app.search.encoder import EmbeddingMetadata
from app.tools.item_picker import item_picker


@dataclass
class FakeEncoder:
    fail: bool = False
    metadata: EmbeddingMetadata = EmbeddingMetadata("fake/bge-m3", "abc123", 3)

    def encode_query(self, text: str) -> list[float]:
        if self.fail:
            raise RuntimeError("encoder unavailable")
        return [1.0, 0.0, 0.0]

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


class FakeIndices:
    def __init__(self, exists: bool = True) -> None:
        self._exists = exists

    def exists(self, *, index: str) -> bool:
        return self._exists

    def get_mapping(self, *, index: str) -> dict:
        return {
            "globuy-category-v1-build": {
                "mappings": {
                    "_meta": {
                        "embedding_model": "fake/bge-m3",
                        "embedding_revision": "abc123",
                        "embedding_dimensions": 3,
                        "embedding_normalized": True,
                        "semantic_text_version": "category-card-v1",
                        "card_schema_version": "category-card-v1",
                        "build_id": "build-1",
                    }
                }
            }
        }


def _cards() -> list[CategoryCard]:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    return [
        CategoryCard(
            card_id="best",
            category="耳机",
            card_type="bestseller",
            summary="京东热销候选 A，价格 299 元",
            raw_evidence=["jingdong|1|销量=100|评分=4.8|价=299.00"],
            last_updated=now,
            confidence=0.9,
        ),
        CategoryCard(
            card_id="price",
            category="耳机",
            card_type="price_range",
            summary="入门 99-299 / 中端 299-699 / 高端 699-1299",
            raw_evidence=["有效CNY挂牌价=10/10；q33=299；q67=699"],
            last_updated=now,
            confidence=1.0,
        ),
        CategoryCard(
            card_id="attr",
            category="耳机",
            card_type="attribute",
            summary="连接方式：蓝牙/无线 80% / 有线 20%",
            raw_evidence=["样本=10；可识别=10；蓝牙/无线=8；有线=2"],
            last_updated=now,
            confidence=1.0,
        ),
    ]


class FakeClient:
    def __init__(self, cards: list[CategoryCard], *, exists: bool = True) -> None:
        self.indices = FakeIndices(exists)
        self.cards = cards
        self.last_search: dict | None = None
        self.search_calls = 0

    def ping(self) -> bool:
        return True

    def search(self, **kwargs) -> dict:
        self.search_calls += 1
        self.last_search = kwargs
        return {
            "hits": {
                "hits": [
                    {"_score": 1.0 - index / 10, "_source": card.model_dump(mode="json")}
                    for index, card in enumerate(self.cards)
                ]
            }
        }


class FakeExtractor:
    prompt_version = "test-prompt-v1"

    async def extract_insight(
        self, query: str, depth: str, cards: list[CategoryCard]
    ) -> InsightExtractionPayload:
        return InsightExtractionPayload(
            components=["耳塞", "充电盒"],
            bestsellers=[
                Bestseller(
                    name="候选 A",
                    typical_price_cny=299,
                    why_popular="卡片显示销量和评分较高",
                    platform="jingdong",
                )
            ],
            attributes=(
                [AttributeDist(name="连接方式", distribution={"蓝牙/无线": 0.8, "有线": 0.2})]
                if depth == "deep"
                else []
            ),
            price_tiers=[
                PriceTier(tier="budget", range_cny=(99, 299), notes="当前快照挂牌价")
            ],
        )


class MissingExtractor:
    prompt_version = "missing-v1"

    async def extract_insight(self, query, depth, cards):
        raise CategoryExtractionNotConfigured("DeepSeek 未配置")


class FakeReranker:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def score(self, query: str, candidates: list[str]) -> list[float]:
        if self.fail:
            raise RuntimeError("reranker unavailable")
        return [float(index) for index, _ in enumerate(candidates)]


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, CategoryInsightOutput] = {}

    async def get(self, key: str) -> CategoryInsightOutput | None:
        value = self.values.get(key)
        return value.model_copy(update={"cache_hit": True}) if value else None

    async def set(self, key: str, value: CategoryInsightOutput, ttl: int) -> bool:
        self.values[key] = value
        return True


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        embedding_model_name="fake/bge-m3",
        embedding_model_revision="abc123",
        embedding_dimensions=3,
        category_quick_k=2,
        category_deep_k=3,
        category_coarse_k=5,
        redis_url=None,
        **overrides,
    )


def _aliases() -> CategoryAliases:
    return CategoryAliases("test-v1", {"耳机": ("耳机", "headphones")})


@pytest.mark.asyncio
async def test_category_service_hybrid_rerank_and_cache() -> None:
    client = FakeClient(_cards())
    cache = MemoryCache()
    service = CategorySearchService(
        client,
        FakeEncoder(),
        FakeExtractor(),
        FakeReranker(),
        cache,
        _aliases(),
        _settings(),
    )

    first, trace = await service.query_with_trace("蓝牙耳机", "quick")
    second, second_trace = await service.query_with_trace("蓝牙耳机", "quick")

    assert first.status == "ok"
    assert first.retrieval_mode == "balanced"
    assert first.attributes == []
    assert "raw_evidence" not in first.model_dump()
    assert trace.rerank_used is True
    assert client.last_search["params"] == {
        "search_pipeline": "globuy-category-balanced-v1"
    }
    assert second.cache_hit is True
    assert second_trace.cache_hit is True
    assert client.search_calls == 1


@pytest.mark.asyncio
async def test_category_service_falls_back_to_bm25_when_embedding_fails() -> None:
    client = FakeClient(_cards())
    service = CategorySearchService(
        client,
        FakeEncoder(fail=True),
        FakeExtractor(),
        FakeReranker(),
        MemoryCache(),
        _aliases(),
        _settings(),
    )

    result, trace = await service.query_with_trace("耳机", "deep")

    assert result.status == "partial"
    assert result.retrieval_mode == "bm25"
    assert trace.degraded_reason == "embedding_unavailable"
    assert "params" not in client.last_search
    assert result.attributes


@pytest.mark.asyncio
async def test_category_service_reports_cold_start_without_fabrication() -> None:
    service = CategorySearchService(
        FakeClient(_cards(), exists=False),
        FakeEncoder(),
        FakeExtractor(),
        FakeReranker(),
        MemoryCache(),
        _aliases(),
        _settings(),
    )

    result = await service.query("耳机", "quick")

    assert result.status == "not_configured"
    assert result.confidence == 0
    assert result.needs_external_validation is True
    assert result.bestsellers == []


@pytest.mark.asyncio
async def test_unknown_category_does_not_search_or_infer() -> None:
    client = FakeClient(_cards())
    service = CategorySearchService(
        client,
        FakeEncoder(),
        FakeExtractor(),
        FakeReranker(),
        MemoryCache(),
        _aliases(),
        _settings(),
    )

    result = await service.query("洗衣机", "quick")

    assert result.status == "insufficient_data"
    assert client.search_calls == 0


@pytest.mark.asyncio
async def test_required_extractor_reports_not_configured() -> None:
    service = CategorySearchService(
        FakeClient(_cards()),
        FakeEncoder(),
        MissingExtractor(),
        FakeReranker(),
        MemoryCache(),
        _aliases(),
        _settings(),
    )

    result = await service.query("耳机", "quick")

    assert result.status == "not_configured"
    assert result.bestsellers == []


def test_cache_key_changes_with_build_and_prompt_versions() -> None:
    base = {
        "category": "耳机",
        "query": "蓝牙耳机",
        "depth": "quick",
    }
    first = category_cache_key(
        **base, index_build_id="build-1", prompt_version="prompt-1"
    )
    second = category_cache_key(
        **base, index_build_id="build-2", prompt_version="prompt-1"
    )
    third = category_cache_key(
        **base, index_build_id="build-1", prompt_version="prompt-2"
    )

    assert len({first, second, third}) == 3
    assert "蓝牙耳机" not in first


@pytest.mark.asyncio
async def test_reranker_ties_keep_coarse_order() -> None:
    class TiedReranker:
        async def score(self, query: str, candidates: list[str]) -> list[float]:
            return [0.5] * len(candidates)

    cards = _cards()
    result = await rerank_cards(TiedReranker(), query="耳机", cards=cards, top_k=2)

    assert [card.card_id for card in result] == ["best", "price"]


def test_item_picker_category_context_only_adds_annotations() -> None:
    candidates = [
        {
            "item_id": "a",
            "platform": "taobao",
            "title": "蓝牙耳机",
            "retrieval_rank": 2,
            "price": 100,
        },
        {
            "item_id": "b",
            "platform": "jingdong",
            "title": "有线耳机",
            "retrieval_rank": 1,
            "price": 200,
        },
    ]
    context = {
        "status": "ok",
        "category": "耳机",
        "depth": "deep",
        "price_tiers": [
            {"tier": "budget", "range_cny": [99, 299], "notes": "snapshot"}
        ],
        "attributes": [
            {"name": "连接方式", "distribution": {"蓝牙": 0.8, "有线": 0.2}}
        ],
        "components": [],
        "retrieval_mode": "hybrid",
    }

    result = item_picker.invoke(
        {"items": candidates, "limit": 2, "category_context": context}
    )

    assert [item["item_id"] for item in result["picks"]] == ["b", "a"]
    assert result["picks"][0]["category_annotations"]["price_tier"] == "budget"
    assert "score" not in result["picks"][0]
