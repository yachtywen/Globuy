import importlib

import pytest

from app.config import Settings
from app.products.realtime import RealtimeCandidateCache
from app.search.schemas import Candidate

item_search_module = importlib.import_module("app.tools.item_search")


def _candidate() -> Candidate:
    return Candidate(
        item_id="jingdong:123", platform="jingdong", title="实时降噪耳机",
        price=399, currency="CNY", retrieval_rank=1, source_kind="realtime_provider",
    )


def test_realtime_cache_is_keyed_by_query_and_platform() -> None:
    cache = RealtimeCandidateCache()
    cache.put("降噪耳机", "jingdong", [_candidate()], 60)
    assert cache.get("降噪耳机", "jingdong") is not None
    assert cache.get("降噪耳机", "taobao") is None


@pytest.mark.asyncio
async def test_item_search_returns_cache_and_starts_background_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
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
        return ItemSearchOutput(status="ok", platform=platform, candidates=candidates, source_kind="hybrid_realtime_catalog", data_as_of=data_as_of)
    monkeypatch.setattr(item_search_module, "_hybridize_realtime_candidates", hybrid)
    result = await item_search_module.item_search.ainvoke({"query": "降噪耳机", "platform": "jingdong", "top_k": 5})
    assert result["status"] == "ok"
    assert result["cache_hit"] is True
    assert result["source_kind"] == "hybrid_realtime_catalog"
    assert result["candidates"][0]["source_kind"] == "realtime_provider"


@pytest.mark.asyncio
async def test_item_search_labels_offline_fallback_when_live_provider_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(realtime_product_provider="none")
    monkeypatch.setattr(item_search_module, "get_settings", lambda: settings)

    class Search:
        def search(self, *_args):
            from app.search.schemas import ItemSearchOutput
            return ItemSearchOutput(status="ok", platform="jingdong", candidates=[_candidate()])

    monkeypatch.setattr(item_search_module, "get_product_search_service", lambda: Search())
    result = await item_search_module.item_search.ainvoke({"query": "降噪耳机", "platform": "jingdong", "top_k": 5})
    assert result["source_kind"] == "offline_snapshot"
    assert "离线快照" in result["message"]
