import importlib

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.database.models import Base, Offer, OfferObservation, OutboxEvent, Product, SourceSnapshot
from app.database.session import Database
from app.products.realtime import RealtimeCandidateCache
from app.products.realtime_catalog import persist_realtime_candidates
from app.search.schemas import Candidate

item_search_module = importlib.import_module("app.tools.item_search")
realtime_module = importlib.import_module("app.products.realtime")


def _candidate() -> Candidate:
    return Candidate(
        item_id="jingdong:123",
        platform="jingdong",
        title="实时降噪耳机",
        price=399,
        currency="CNY",
        retrieval_rank=1,
        source_kind="realtime_provider",
    )


def test_douyin_candidate_retains_promotion_identity() -> None:
    row = {
        "product_id": "product-1",
        "promotion_id": "promotion-1",
        "base_model": {
            "product_info": {"name": "实时耳机"},
            "marketing_info": {"price_desc": {"price": {"origin": 15900}}},
        },
    }
    candidate = realtime_module._candidate(row, "douyin", "2026-07-25T00:00:00+00:00")
    assert candidate is not None
    assert candidate.item_id == "douyin:product-1"
    assert candidate.attributes["promotion_id"] == "promotion-1"


def test_realtime_cache_is_keyed_by_query_and_platform() -> None:
    cache = RealtimeCandidateCache()
    cache.put("降噪耳机", "jingdong", [_candidate()], 60)
    assert cache.get("降噪耳机", "jingdong") is not None
    assert cache.get("降噪耳机", "taobao") is None


@pytest.mark.asyncio
async def test_item_search_returns_cache_and_starts_background_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(realtime_product_provider="justone", justone_api_token="test-token")
    cache = RealtimeCandidateCache()
    cache.put("降噪耳机", "jingdong", [_candidate()], 60)
    monkeypatch.setattr(item_search_module, "get_settings", lambda: settings)
    monkeypatch.setattr(item_search_module, "_realtime_cache", cache)

    async def no_refresh(*_args):
        return None

    monkeypatch.setattr(item_search_module, "_refresh_realtime_cache", no_refresh)

    async def hybrid(_query, platform, candidates, _top_k, _filters, data_as_of):
        from app.search.schemas import ItemSearchOutput

        return ItemSearchOutput(
            status="ok",
            platform=platform,
            candidates=candidates,
            source_kind="hybrid_realtime_catalog",
            data_as_of=data_as_of,
        )

    monkeypatch.setattr(item_search_module, "_hybridize_realtime_candidates", hybrid)
    result = await item_search_module.item_search.ainvoke(
        {"query": "降噪耳机", "platform": "jingdong", "top_k": 5}
    )
    assert result["status"] == "ok"
    assert result["cache_hit"] is True
    assert result["source_kind"] == "hybrid_realtime_catalog"
    assert result["candidates"][0]["source_kind"] == "realtime_provider"


@pytest.mark.asyncio
async def test_item_search_labels_offline_fallback_when_live_provider_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(realtime_product_provider="none")
    monkeypatch.setattr(item_search_module, "get_settings", lambda: settings)

    class Search:
        def search(self, *_args):
            from app.search.schemas import ItemSearchOutput

            return ItemSearchOutput(status="ok", platform="jingdong", candidates=[_candidate()])

    monkeypatch.setattr(item_search_module, "get_product_search_service", lambda: Search())
    result = await item_search_module.item_search.ainvoke(
        {"query": "降噪耳机", "platform": "jingdong", "top_k": 5}
    )
    assert result["source_kind"] == "offline_snapshot"
    assert "离线快照" in result["message"]


@pytest.mark.asyncio
async def test_realtime_candidates_are_persisted_idempotently_before_wishlist(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/catalog.sqlite3")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    candidate = Candidate(
        item_id="jingdong:123",
        platform="jingdong",
        title="实时降噪耳机",
        price=399,
        currency="CNY",
        rating=4.8,
        sales=1200,
        image_url="https://img10.360buyimg.com/n1/jfs/example.jpg",
        product_url="https://item.jd.com/123.html",
        attributes={"shop_name": "测试店铺", "brand": "测试品牌"},
        retrieval_rank=1,
        source_kind="realtime_provider",
        data_as_of="2026-07-25T08:00:00+00:00",
        wishlist_eligible=False,
    )
    try:
        first = await persist_realtime_candidates(
            database,
            provider="justone",
            query="降噪耳机",
            platform="jingdong",
            candidates=[candidate],
        )
        second = await persist_realtime_candidates(
            database,
            provider="justone",
            query="降噪耳机",
            platform="jingdong",
            candidates=[candidate],
        )
        assert first[0].wishlist_eligible is True
        assert first[0].offer_id == second[0].offer_id
        async with database.sessions() as session:
            for model in (Product, Offer, SourceSnapshot, OfferObservation, OutboxEvent):
                assert await session.scalar(select(func.count()).select_from(model)) == 1
            offer = await session.get(Offer, first[0].offer_id)
            assert offer is not None
            assert float(offer.current_price) == 399
            assert offer.image_url == candidate.image_url
            assert offer.product_url == candidate.product_url
    finally:
        await database.close()
