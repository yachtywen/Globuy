"""Single-platform product search through Hybrid or direct catalog strategy."""

from __future__ import annotations

import asyncio
import hashlib
from functools import lru_cache
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.api.monitor import current_monitor
from app.config import get_settings
from app.database.session import Database
from app.infrastructure.opensearch import build_opensearch_client
from app.products.catalog.coverage import CatalogCoverageService
from app.products.catalog.hydration import CatalogHydrationCoordinator
from app.products.catalog.intent import ShoppingIntent
from app.products.catalog.repository import CatalogRepository
from app.products.catalog.scope import CatalogScope
from app.products.outbox_worker import ProductOutboxWorker
from app.products.providers.justone import JustOneProvider
from app.search.encoder import get_embedding_encoder
from app.search.schemas import ItemSearchOutput, Platform, SearchFilters
from app.search.service import ProductSearchService, SearchNotConfiguredError
from app.utils.thread_ctx import current_thread_id, current_user_id


@lru_cache(maxsize=1)
def get_product_search_service() -> ProductSearchService:
    settings = get_settings()
    return ProductSearchService(
        build_opensearch_client(settings), get_embedding_encoder(), settings
    )


@lru_cache(maxsize=1)
def get_catalog_runtime() -> tuple[CatalogHydrationCoordinator, ProductOutboxWorker]:
    settings = get_settings()
    if settings.database_url is None:
        raise SearchNotConfiguredError("PostgreSQL 商品目录尚未配置")
    database = Database(
        settings.database_url.get_secret_value(),
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        pool_recycle=settings.database_pool_recycle_seconds,
    )
    repository = CatalogRepository(database, scope_ttl_seconds=settings.catalog_scope_ttl_seconds)
    coordinator = CatalogHydrationCoordinator(
        JustOneProvider(settings),
        CatalogCoverageService(
            database,
            freshness_seconds=settings.catalog_freshness_seconds,
            minimum=settings.catalog_minimum_per_platform,
        ),
        repository,
        settings,
    )
    return coordinator, ProductOutboxWorker(database, batch_size=settings.product_outbox_batch_size)


def resolve_search_strategy() -> str:
    settings = get_settings()
    if settings.item_search_strategy != "progressive":
        return settings.item_search_strategy
    identity = current_user_id() or current_thread_id() or "anonymous"
    bucket = int(hashlib.sha256(identity.encode()).hexdigest()[:8], 16) % 100
    return "direct_llm" if bucket < settings.direct_rerank_rollout_percent else "hybrid"


@tool
async def item_search(
    query: Annotated[str, Field(min_length=1, max_length=500)],
    platform: Platform,
    top_k: Annotated[int, Field(ge=1, le=50)] = 20,
    filters: SearchFilters | None = None,
    intent: ShoppingIntent | None = None,
) -> dict:
    """Search one platform using the configured Hybrid or direct strategy."""

    normalized_query = query.strip()
    try:
        settings = get_settings()
        strategy = resolve_search_strategy()
        hydration = None
        if intent is not None:
            if intent.needs_clarification:
                return ItemSearchOutput(
                    status="partial",
                    platform=platform,
                    message=intent.clarification_question or "需要补充商品品类信息",
                    catalog_status="partial",
                    provider_status="blocked",
                ).model_dump(mode="json")
            coordinator = worker = None
            if settings.product_provider != "none" or strategy == "direct_llm":
                coordinator, worker = get_catalog_runtime()
            if settings.product_provider != "none" and coordinator is not None:
                # ItemSearch is deliberately single-platform. Dispatch may execute one
                # call per platform concurrently, so hydrating the original multi-platform
                # intent here would make those calls race over the same scopes and rows.
                platform_intent = intent.model_copy(update={"platforms": [platform]})
                if strategy == "direct_llm":
                    hydration = await coordinator.ensure(
                        platform_intent,
                        target_total=settings.direct_candidates_per_platform,
                    )
                else:
                    hydration = await coordinator.ensure(platform_intent)
            if strategy == "hybrid" and hydration is not None and hydration.offer_ids:
                projected = await worker.run_once(hydration.offer_ids)
                monitor = current_monitor()
                if monitor is not None:
                    await monitor.report_catalog(
                        "catalog_index_progress",
                        phase="indexing",
                        status="finished",
                        embedded=projected.get("embedded", 0),
                        reused_vectors=projected.get("reused_vectors", 0),
                        indexed=projected.get("published", 0),
                        message="已建立商品语义检索目录",
                    )
        active_filters = filters or (intent.filters if intent else None)
        if strategy == "direct_llm":
            if intent is None:
                output = ItemSearchOutput(
                    status="partial",
                    platform=platform,
                    message="直搜链需要结构化 ShoppingIntent",
                    provider_status="blocked",
                    search_strategy="direct_llm",
                )
            else:
                coordinator, _ = get_catalog_runtime()
                scope_provider = (
                    settings.product_provider if settings.product_provider != "none" else "justone"
                )
                scope = CatalogScope.from_intent(intent, platform, provider=scope_provider)
                candidates = await coordinator.repository.load_scope_candidates(
                    scope,
                    filters=active_filters,
                    limit=min(top_k, settings.direct_candidates_per_platform),
                )
                count = hydration.platform_counts.get(platform, 0) if hydration else len(candidates)
                status = (
                    "ok"
                    if candidates
                    else ("not_configured" if settings.product_provider == "none" else "partial")
                )
                output = ItemSearchOutput(
                    status=status,
                    platform=platform,
                    candidates=candidates,
                    total_recall=count,
                    truncated=count > len(candidates),
                    message=(
                        "本地目录没有该品类的新鲜候选，实时商品 Provider 当前未配置"
                        if status == "not_configured"
                        else None
                    ),
                    catalog_status=("hydrated" if hydration and hydration.total else "fresh"),
                    catalog_candidate_count=count,
                    provider_status=(
                        hydration.provider_status
                        if hydration
                        else "not_configured"
                        if settings.product_provider == "none"
                        else None
                    ),
                    search_strategy="direct_llm",
                )
        else:
            output = await asyncio.to_thread(
                get_product_search_service().search,
                normalized_query,
                platform,
                min(top_k, settings.fork_candidate_limit),
                active_filters,
                category_key=intent.category_key if intent else None,
                catalog_status=("hydrated" if hydration and hydration.total else "fresh")
                if intent
                else None,
                catalog_candidate_count=(
                    hydration.platform_counts.get(platform, 0) if hydration else 0
                ),
                provider_status=(
                    hydration.provider_status
                    if hydration
                    else "not_configured"
                    if intent and settings.product_provider == "none"
                    else None
                ),
            )
        if hydration and hydration.status == "partial":
            output.status = "partial"
        if intent and settings.product_provider == "none" and output.catalog_candidate_count == 0:
            output.status = "not_configured"
            output.catalog_status = "stale"
            output.message = "本地目录尚未覆盖该品类，实时商品 Provider 当前未配置"
        monitor = current_monitor()
        if monitor is not None:
            await monitor.report_catalog(
                "hybrid_retrieval_progress",
                phase="retrieval",
                status="finished",
                candidate_pool=output.total_recall,
                returned=len(output.candidates),
                strategy=output.search_strategy,
                message=(
                    "已完成结构化候选读取"
                    if output.search_strategy == "direct_llm"
                    else "已从候选中完成混合检索"
                ),
            )
    except SearchNotConfiguredError as exc:
        output = ItemSearchOutput(status="not_configured", platform=platform, message=str(exc))
    except Exception as exc:
        output = ItemSearchOutput(status="error", platform=platform, message=str(exc))

    return output.model_dump(mode="json")
