"""Single-platform live product search with hybrid retrieval and safe fallback."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.config import get_settings
from app.products.realtime import (
    RealtimeCandidateCache,
    RealtimeProviderError,
    RealtimeProviderNotConfigured,
    build_realtime_provider,
)
from app.infrastructure.opensearch import build_opensearch_client
from app.search.encoder import get_embedding_encoder
from app.search.schemas import Candidate, ItemSearchOutput, Platform, SearchFilters
from app.search.service import ProductIndexManager, ProductSearchService, SearchNotConfiguredError


@lru_cache(maxsize=1)
def get_product_search_service() -> ProductSearchService:
    settings = get_settings()
    return ProductSearchService(
        build_opensearch_client(settings), get_embedding_encoder(), settings
    )


_realtime_cache = RealtimeCandidateCache()


def _matches_realtime_filters(candidate: Candidate, filters: SearchFilters | None) -> bool:
    """Apply public scalar filters when OpenSearch cannot perform hybrid search."""

    if filters is None:
        return True
    if filters.min_price is not None and candidate.price < filters.min_price:
        return False
    if filters.max_price is not None and candidate.price > filters.max_price:
        return False
    if filters.currency and candidate.currency.upper() != filters.currency.upper():
        return False
    if filters.min_rating is not None and (
        candidate.rating is None or candidate.rating < filters.min_rating
    ):
        return False
    if filters.min_sales is not None and (
        candidate.sales is None or candidate.sales < filters.min_sales
    ):
        return False
    return not filters.attribute_equals or all(
        candidate.attributes.get(key) == value
        for key, value in filters.attribute_equals.items()
    )


def _realtime_only_output(
    platform: Platform,
    candidates: list[Candidate],
    top_k: int,
    filters: SearchFilters | None,
    data_as_of: str,
    message: str,
    *,
    cache_hit: bool = False,
) -> ItemSearchOutput:
    """Return verified live candidates while explicitly reporting RRF degradation."""

    filtered = [item for item in candidates if _matches_realtime_filters(item, filters)]
    ranked = [
        item.model_copy(update={"retrieval_rank": rank, "data_as_of": data_as_of})
        for rank, item in enumerate(filtered[:top_k], start=1)
    ]
    return ItemSearchOutput(
        status="ok",
        platform=platform,
        candidates=ranked,
        total_recall=len(filtered),
        truncated=len(filtered) > len(ranked),
        message=message,
        cache_hit=cache_hit,
        data_as_of=data_as_of,
        source_kind="realtime_provider",
    )


async def _refresh_realtime_cache(query: str, platform: Platform) -> None:
    """Best-effort stale-while-revalidate refresh; failures retain last good data."""

    settings = get_settings()
    try:
        candidates = await build_realtime_provider(settings).search(
            query, platform, settings.realtime_search_candidate_limit
        )
        _realtime_cache.put(query, platform, candidates, settings.realtime_search_cache_ttl_seconds)
    except RealtimeProviderError:
        return


async def _hybridize_realtime_candidates(
    query: str,
    platform: Platform,
    candidates: list[Candidate],
    top_k: int,
    filters: SearchFilters | None,
    data_as_of: str,
) -> ItemSearchOutput:
    """Publish normalized live candidates, then use the established RRF path."""

    service = get_product_search_service()
    settings = get_settings()
    manager = ProductIndexManager(service.client, service.encoder, settings)
    documents = [candidate.model_dump(mode="json") for candidate in candidates]
    await asyncio.to_thread(manager.ensure_pipeline)
    await asyncio.to_thread(manager.ensure_index)
    alias = settings.opensearch_product_alias
    alias_exists = await asyncio.to_thread(manager.client.indices.exists_alias, name=alias)
    if not alias_exists:
        await asyncio.to_thread(
            manager.client.indices.update_aliases,
            body={"actions": [{"add": {"index": settings.opensearch_product_index, "alias": alias}}]},
        )
    await asyncio.to_thread(manager.index_items, documents)
    output = await asyncio.to_thread(service.search, query, platform, top_k, filters)
    live_ids = {candidate.item_id for candidate in candidates}
    ranked = [
        candidate.model_copy(
            update={
                "source_kind": "realtime_provider" if candidate.item_id in live_ids else "offline_snapshot",
                "data_as_of": data_as_of if candidate.item_id in live_ids else None,
            }
        )
        for candidate in output.candidates
    ]
    return output.model_copy(
        update={
            "candidates": ranked,
            "source_kind": "hybrid_realtime_catalog",
            "data_as_of": data_as_of,
            "message": "实时商品已完成混合检索。",
        }
    )


async def _offline_fallback(
    query: str, platform: Platform, top_k: int, filters: SearchFilters | None, reason: str
) -> ItemSearchOutput:
    """Keep discovery usable while labeling an unavailable live provider."""

    try:
        output = await asyncio.to_thread(
            get_product_search_service().search, query, platform, top_k, filters
        )
        return output.model_copy(
            update={
                "source_kind": "offline_snapshot",
                "message": f"实时商品源不可用（{reason}）；当前展示离线快照。",
            }
        )
    except SearchNotConfiguredError as exc:
        return ItemSearchOutput(
            status="not_configured",
            platform=platform,
            source_kind="offline_snapshot",
            message=f"{reason}；离线检索也不可用：{exc}",
        )
    except Exception as exc:
        return ItemSearchOutput(
            status="error",
            platform=platform,
            source_kind="offline_snapshot",
            message=f"{reason}；离线检索失败：{exc}",
        )


@tool
async def item_search(
    query: Annotated[str, Field(min_length=1, max_length=500)],
    platform: Platform,
    top_k: Annotated[int, Field(ge=1, le=50)] = 20,
    filters: SearchFilters | None = None,
) -> dict:
    """Search one commerce platform using live candidates and hybrid retrieval."""

    normalized_query = query.strip()
    settings = get_settings()
    if settings.realtime_product_provider != "none":
        cached = _realtime_cache.get(normalized_query, platform)
        if cached is not None:
            asyncio.create_task(_refresh_realtime_cache(normalized_query, platform))
            try:
                output = await _hybridize_realtime_candidates(
                    normalized_query, platform, cached.candidates, top_k, filters, cached.data_as_of
                )
                return output.model_copy(update={"cache_hit": True}).model_dump(mode="json")
            except Exception as exc:
                return _realtime_only_output(
                    platform,
                    cached.candidates,
                    top_k,
                    filters,
                    cached.data_as_of,
                    f"实时商品缓存可用；混合检索暂不可用，已直接展示实时结果：{exc}",
                    cache_hit=True,
                ).model_dump(mode="json")
        try:
            candidates = await build_realtime_provider(settings).search(
                normalized_query, platform, min(top_k, settings.realtime_search_candidate_limit)
            )
            cached = _realtime_cache.put(
                normalized_query, platform, candidates, settings.realtime_search_cache_ttl_seconds
            )
            try:
                return (
                    await _hybridize_realtime_candidates(
                        normalized_query, platform, candidates, top_k, filters, cached.data_as_of
                    )
                ).model_dump(mode="json")
            except Exception as exc:
                return _realtime_only_output(
                    platform,
                    candidates,
                    top_k,
                    filters,
                    cached.data_as_of,
                    f"已取得实时商品；混合检索暂不可用，已直接展示实时结果：{exc}",
                ).model_dump(mode="json")
        except RealtimeProviderNotConfigured as exc:
            return ItemSearchOutput(
                status="not_configured",
                platform=platform,
                message=str(exc),
                source_kind="realtime_provider",
            ).model_dump(mode="json")
        except RealtimeProviderError as exc:
            return (
                await _offline_fallback(normalized_query, platform, top_k, filters, str(exc))
            ).model_dump(mode="json")
    return (
        await _offline_fallback(
            normalized_query, platform, top_k, filters, "实时商品 Provider 未启用"
        )
    ).model_dump(mode="json")
