"""Single-platform product search over the local OpenSearch catalog."""

from __future__ import annotations

import asyncio
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
from app.products.outbox_worker import ProductOutboxWorker
from app.products.providers.justone import JustOneProvider
from app.search.encoder import get_embedding_encoder
from app.search.schemas import ItemSearchOutput, Platform, SearchFilters
from app.search.service import ProductSearchService, SearchNotConfiguredError


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
        raise SearchNotConfiguredError("MySQL 商品目录尚未配置")
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


@tool
async def item_search(
    query: Annotated[str, Field(min_length=1, max_length=500)],
    platform: Platform,
    top_k: Annotated[int, Field(ge=1, le=50)] = 20,
    filters: SearchFilters | None = None,
    intent: ShoppingIntent | None = None,
) -> dict:
    """Search one commerce platform using BM25 and frozen dense-vector retrieval."""

    normalized_query = query.strip()
    try:
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
            settings = get_settings()
            if settings.product_provider != "none":
                coordinator, worker = get_catalog_runtime()
                # ItemSearch is deliberately single-platform. Dispatch may execute one
                # call per platform concurrently, so hydrating the original multi-platform
                # intent here would make those calls race over the same scopes and rows.
                platform_intent = intent.model_copy(update={"platforms": [platform]})
                hydration = await coordinator.ensure(platform_intent)
            if hydration is not None and hydration.offer_ids:
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
        output = await asyncio.to_thread(
            get_product_search_service().search,
            normalized_query,
            platform,
            min(top_k, get_settings().fork_candidate_limit),
            filters or (intent.filters if intent else None),
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
                if intent and get_settings().product_provider == "none"
                else None
            ),
        )
        if hydration and hydration.status == "partial":
            output.status = "partial"
        if (
            intent
            and get_settings().product_provider == "none"
            and output.catalog_candidate_count == 0
        ):
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
                message="已从候选中完成混合检索",
            )
    except SearchNotConfiguredError as exc:
        output = ItemSearchOutput(status="not_configured", platform=platform, message=str(exc))
    except Exception as exc:
        output = ItemSearchOutput(status="error", platform=platform, message=str(exc))

    return output.model_dump(mode="json")
