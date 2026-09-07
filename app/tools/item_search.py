"""Single-platform product search backed by PostgreSQL candidates and FAISS reranking."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.api.monitor import current_monitor
from app.config import get_settings
from app.database.session import Database
from app.products.catalog.coverage import CatalogCoverageService
from app.products.catalog.hydration import CatalogHydrationCoordinator
from app.products.catalog.intent import ShoppingIntent
from app.products.catalog.repository import CatalogRepository
from app.products.catalog.scope import CatalogScope
from app.products.providers.justone import JustOneProvider
from app.search.errors import SearchNotConfiguredError
from app.search.schemas import ItemSearchOutput, Platform, SearchFilters


@lru_cache(maxsize=1)
def get_catalog_runtime() -> CatalogHydrationCoordinator:
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
    return coordinator


@tool
async def item_search(
    query: Annotated[str, Field(min_length=1, max_length=500)],
    platform: Platform,
    top_k: Annotated[int, Field(ge=1, le=50)] = 20,
    filters: SearchFilters | None = None,
    intent: ShoppingIntent | None = None,
) -> dict:
    """Read one platform's verified candidates for the request-local FAISS chain."""

    try:
        settings = get_settings()
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
            coordinator = get_catalog_runtime()
            if settings.product_provider != "none" and coordinator is not None:
                # ItemSearch is deliberately single-platform. Dispatch may execute one
                # call per platform concurrently, so hydrating the original multi-platform
                # intent here would make those calls race over the same scopes and rows.
                platform_intent = intent.model_copy(update={"platforms": [platform]})
                hydration = await coordinator.ensure(
                    platform_intent,
                    target_total=settings.faiss_candidates_per_platform,
                )
        active_filters = filters or (intent.filters if intent else None)
        if intent is None:
            output = ItemSearchOutput(
                status="partial",
                platform=platform,
                message="FAISS 搜索链需要结构化 ShoppingIntent",
                provider_status="blocked",
            )
        else:
            scope_provider = (
                settings.product_provider if settings.product_provider != "none" else "justone"
            )
            scope = CatalogScope.from_intent(intent, platform, provider=scope_provider)
            candidates = await coordinator.repository.load_scope_candidates(
                scope,
                filters=active_filters,
                limit=min(top_k, settings.faiss_candidates_per_platform),
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
                retrieval_route=(
                    "exact_direct" if intent.intent_mode == "exact_product" else "category_faiss"
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
                "faiss_retrieval_progress",
                phase="retrieval",
                status="finished",
                candidate_pool=output.total_recall,
                returned=len(output.candidates),
                strategy="faiss",
                message="已读取 FAISS 重排所需的结构化候选",
            )
    except SearchNotConfiguredError as exc:
        output = ItemSearchOutput(status="not_configured", platform=platform, message=str(exc))
    except Exception as exc:
        output = ItemSearchOutput(status="error", platform=platform, message=str(exc))

    return output.model_dump(mode="json")
