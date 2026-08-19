"""Shared multi-platform catalog hydration with subscriber-safe cancellation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from app.api.monitor import Monitor, current_monitor
from app.config import Settings, get_settings
from app.products.catalog.coverage import CatalogCoverageService
from app.products.catalog.intent import ShoppingIntent
from app.products.catalog.repository import CatalogRepository
from app.products.catalog.scope import CatalogScope, ProviderRequestFingerprint
from app.products.catalog.stop_policy import CatalogStopPolicy, StopReason
from app.products.providers.base import (
    ProductProvider,
    ProviderErrorCode,
    ProviderPage,
    ProviderSearchRequest,
)
from app.products.providers.normalization import normalize_item
from app.search.schemas import Platform
from app.utils.thread_ctx import current_run_id, current_thread_id


@dataclass(frozen=True)
class HydrationResult:
    status: str
    total: int
    platform_counts: dict[str, int]
    partial_platforms: list[str]
    stop_reason: str
    provider_status: str
    offer_ids: tuple[str, ...] = ()


@dataclass
class _Subscriber:
    monitor: Monitor | None
    thread_id: str
    run_id: str


@dataclass
class _SharedJob:
    task: asyncio.Task[HydrationResult]
    subscribers: dict[tuple[str, str], _Subscriber] = field(default_factory=dict)
    last_progress_emit: dict[str, float] = field(default_factory=dict)


class CatalogHydrationCoordinator:
    def __init__(
        self,
        provider: ProductProvider,
        coverage: CatalogCoverageService,
        repository: CatalogRepository,
        settings: Settings | None = None,
    ) -> None:
        self.provider = provider
        self.coverage = coverage
        self.repository = repository
        self.settings = settings or get_settings()
        self._jobs: dict[str, _SharedJob] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def group_key(intent: ShoppingIntent, provider: str) -> str:
        value = {
            "category_key": intent.category_key,
            "query": " ".join(intent.primary_query.lower().split()),
            "platforms": sorted(intent.platforms),
            "filters": intent.filters.model_dump(mode="json"),
            "provider": provider,
            "version": "hydration-v1",
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    async def ensure(self, intent: ShoppingIntent) -> HydrationResult:
        if not intent.provider_allowed:
            return HydrationResult(
                "partial", 0, {}, list(intent.platforms), "clarification", "blocked"
            )
        provider_name = self.settings.product_provider
        if provider_name == "none":
            return HydrationResult(
                "not_configured",
                0,
                {},
                list(intent.platforms),
                "provider_disabled",
                "not_configured",
            )
        thread_id, run_id = current_thread_id(), current_run_id()
        if thread_id is None or run_id is None:
            raise RuntimeError("Catalog hydration requires thread/run context")
        key = self.group_key(intent, provider_name)
        subscriber_key = (thread_id.split("-fork-", 1)[0], run_id)
        async with self._lock:
            job = self._jobs.get(key)
            if job is None or job.task.done():
                placeholder: dict[str, _SharedJob] = {}
                task = asyncio.create_task(self._run(key, intent, placeholder))
                job = _SharedJob(task=task)
                placeholder["job"] = job
                self._jobs[key] = job
            job.subscribers[subscriber_key] = _Subscriber(current_monitor(), *subscriber_key)
        try:
            return await asyncio.shield(job.task)
        finally:
            async with self._lock:
                current = self._jobs.get(key)
                if current is job:
                    current.subscribers.pop(subscriber_key, None)
                    if not current.subscribers and not current.task.done():
                        current.task.cancel()
                    if current.task.done() and not current.subscribers:
                        self._jobs.pop(key, None)

    async def _emit(self, holder: dict[str, _SharedJob], name: str, **data: Any) -> None:
        job = holder.get("job")
        if job is None:
            return
        if name == "catalog_fetch_progress":
            progress_key = str(data.get("platform") or "all")
            now = time.monotonic()
            if now - job.last_progress_emit.get(progress_key, 0) < 0.35:
                return
            job.last_progress_emit[progress_key] = now
        for subscriber in list(job.subscribers.values()):
            if subscriber.monitor is not None:
                await subscriber.monitor.report_catalog_for(
                    subscriber.thread_id,
                    subscriber.run_id,
                    name,
                    **data,
                )

    async def _run(
        self, key: str, intent: ShoppingIntent, holder: dict[str, _SharedJob]
    ) -> HydrationResult:
        settings = self.settings
        hydration_run_id = uuid4().hex
        scopes = {
            platform: CatalogScope.from_intent(intent, platform, provider=settings.product_provider)
            for platform in intent.platforms
        }
        for scope in scopes.values():
            await self.repository.ensure_scope(scope)
        coverages = await asyncio.gather(
            *(self.coverage.inspect(scope) for scope in scopes.values())
        )
        fresh = {
            platform: coverage.fresh_count
            for platform, coverage in zip(scopes, coverages, strict=True)
        }
        seen_offer_ids = {
            platform: set(coverage.fresh_offer_ids)
            for platform, coverage in zip(scopes, coverages, strict=True)
        }
        await self._emit(
            holder,
            "catalog_cache_checked",
            phase="catalog",
            status="sufficient" if all(not item.refresh_required for item in coverages) else "thin",
            fresh_candidates=sum(fresh.values()),
            target=settings.catalog_target_total,
            message="正在检查已有商品目录",
        )
        if all(not item.refresh_required for item in coverages):
            return HydrationResult(
                "ok", sum(fresh.values()), fresh, [], "cache_sufficient", "not_called"
            )
        await self.repository.create_hydration_run(
            hydration_run_id,
            key,
            intent.model_dump(mode="json"),
            {
                "minimum": settings.catalog_minimum_total,
                "target": settings.catalog_target_total,
                "hard_cap": settings.catalog_hard_cap_total,
                "minimum_per_platform": settings.catalog_minimum_per_platform,
            },
            settings.catalog_lease_seconds,
        )
        leased: set[Platform] = set()
        for platform, scope in scopes.items():
            if await self.repository.acquire_scope_lease(
                scope.scope_id, hydration_run_id, settings.catalog_lease_seconds
            ):
                leased.add(platform)
        policy = CatalogStopPolicy(
            tuple(intent.platforms),
            settings.catalog_minimum_total,
            settings.catalog_target_total,
            settings.catalog_hard_cap_total,
            settings.catalog_minimum_per_platform,
            settings.catalog_max_success_calls_per_run,
            settings.catalog_max_attempts_per_run,
            counts=fresh,
        )
        await self._emit(
            holder,
            "catalog_fetch_started",
            phase="provider_search",
            platforms=intent.platforms,
            minimum=policy.minimum_total,
            target=policy.target_total,
            hard_cap=policy.hard_cap_total,
            message="正在从多个平台搜索商品",
        )
        started = time.monotonic()
        partial: set[str] = set()
        hydrated_offer_ids: set[str] = set()
        cursors: dict[Platform, Any] = {platform: {"page": 1} for platform in intent.platforms}
        variant_used: set[Platform] = set()
        low_yield: dict[Platform, int] = {platform: 0 for platform in intent.platforms}
        platform_locks = {platform: asyncio.Lock() for platform in intent.platforms}
        policy_lock = asyncio.Lock()

        async def fetch(platform: Platform) -> None:
            async with platform_locks[platform]:
                if platform not in leased:
                    partial.add(platform)
                    policy.exhausted.add(platform)
                    return
                while (
                    policy.reason(
                        soft_deadline=time.monotonic() - started
                        >= settings.catalog_soft_deadline_seconds,
                        hard_deadline=time.monotonic() - started
                        >= settings.catalog_hard_deadline_seconds,
                    )
                    is None
                ):
                    keyword = intent.primary_query
                    if (
                        low_yield[platform] >= 2
                        and intent.query_variants
                        and platform not in variant_used
                    ):
                        keyword = intent.query_variants[0]
                        cursors[platform] = {"page": 1}
                        variant_used.add(platform)
                    fingerprint = ProviderRequestFingerprint(
                        scope_id=scopes[platform].scope_id,
                        provider=settings.product_provider,
                        platform=platform,
                        normalized_query=keyword,
                        provider_filters=intent.filters,
                        cursor=cursors[platform],
                    )
                    if not await self.repository.reserve_request(
                        fingerprint, hydration_run_id
                    ):
                        policy.exhausted.add(platform)
                        break
                    request = ProviderSearchRequest(
                        provider=settings.product_provider,
                        platform=platform,
                        keyword=keyword,
                        cursor=fingerprint.cursor,
                        filters=intent.filters,
                        request_key=fingerprint.request_key,
                    )
                    remaining_seconds = max(
                        0.01,
                        settings.catalog_hard_deadline_seconds
                        - (time.monotonic() - started),
                    )
                    try:
                        page = await asyncio.wait_for(
                            self.provider.search(request), timeout=remaining_seconds
                        )
                    except TimeoutError:
                        page = ProviderPage(
                            status=ProviderErrorCode.UNKNOWN,
                            platform=platform,
                            message="商品数据服务响应状态未知",
                        )
                    attempt_count = 1
                    if page.status in {
                        ProviderErrorCode.PROVIDER_ERROR,
                        ProviderErrorCode.RATE_LIMITED,
                    }:
                        delay = 0.1 if page.status == ProviderErrorCode.RATE_LIMITED else 0.05
                        await asyncio.sleep(delay)
                        retry_remaining = (
                            settings.catalog_hard_deadline_seconds
                            - (time.monotonic() - started)
                        )
                        if retry_remaining > 0:
                            try:
                                page = await asyncio.wait_for(
                                    self.provider.search(request), timeout=retry_remaining
                                )
                            except TimeoutError:
                                page = ProviderPage(
                                    status=ProviderErrorCode.UNKNOWN,
                                    platform=platform,
                                    message="商品数据服务响应状态未知",
                                )
                            attempt_count += 1
                    normalized = [
                        item for raw in page.items if (item := normalize_item(platform, raw))
                    ]
                    persisted = {"accepted": 0, "offer_ids": []}
                    async with policy_lock:
                        remaining = max(0, policy.hard_cap_total - policy.total)
                        normalized = normalized[:remaining]
                        if page.status == ProviderErrorCode.OK and normalized:
                            try:
                                persisted = await self.repository.persist_page(
                                    scopes[platform], fingerprint, normalized
                                )
                            except Exception:
                                await self.repository.finish_request(
                                    fingerprint.request_key,
                                    status="persistence_error",
                                    request_id=page.request_id,
                                    duration_ms=page.duration_ms,
                                    attempt_count=attempt_count,
                                )
                                raise
                    await self._emit(
                        holder,
                        "catalog_normalization_progress",
                        phase="normalizing",
                        status="finished",
                        received=len(page.items),
                        accepted=len(normalized),
                        duplicates=0,
                        rejected=max(0, len(page.items) - len(normalized)),
                        message="已完成商品字段校验",
                    )
                    if page.status == ProviderErrorCode.OK:
                        await self._emit(
                            holder,
                            "catalog_persistence_progress",
                            phase="persistence",
                            status="finished",
                            upserted_products=int(persisted["accepted"]),
                            new_observations=int(persisted["accepted"]),
                            message="已保存有效商品信息",
                        )
                    await self.repository.finish_request(
                        fingerprint.request_key,
                        status=page.status.value,
                        request_id=page.request_id,
                        duration_ms=page.duration_ms,
                        attempt_count=attempt_count,
                    )
                    offer_ids = {str(item) for item in persisted.get("offer_ids", [])}
                    async with policy_lock:
                        new_offer_ids = offer_ids - seen_offer_ids[platform]
                        seen_offer_ids[platform].update(offer_ids)
                        hydrated_offer_ids.update(offer_ids)
                    accepted = len(new_offer_ids)
                    low_yield[platform] = low_yield[platform] + 1 if accepted < 3 else 0
                    success = page.status == ProviderErrorCode.OK
                    async with policy_lock:
                        policy.observe(
                            platform, accepted, success=success, has_more=page.has_more
                        )
                    if not success:
                        partial.add(platform)
                        break
                    await self._emit(
                        holder,
                        "catalog_fetch_progress",
                        phase="provider_search",
                        platform=platform,
                        page=request.cursor.page,
                        received=len(page.items),
                        accepted=accepted,
                        platform_total=policy.counts[platform],
                        deduplicated_total=policy.total,
                        target=policy.target_total,
                        status="running",
                        message=f"{platform} 已找到 {policy.counts[platform]} 件有效候选",
                    )
                    if page.next_cursor is None:
                        break
                    cursors[platform] = page.next_cursor

        tasks = [asyncio.create_task(fetch(platform)) for platform in intent.platforms]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.repository.finish_hydration_run(
                hydration_run_id,
                status="cancelled",
                platform_counts=dict(policy.counts),
                stop_reason=StopReason.CANCELLED.value,
            )
            raise
        except Exception:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.repository.finish_hydration_run(
                hydration_run_id,
                status="error",
                platform_counts=dict(policy.counts),
                stop_reason="error",
            )
            raise
        finally:
            await asyncio.gather(
                *(
                    self.repository.release_scope_lease(
                        scopes[platform].scope_id, hydration_run_id
                    )
                    for platform in leased
                ),
                return_exceptions=True,
            )
        reason = (
            policy.reason(
                soft_deadline=time.monotonic() - started >= settings.catalog_soft_deadline_seconds,
                hard_deadline=time.monotonic() - started >= settings.catalog_hard_deadline_seconds,
            )
            or StopReason.EXHAUSTED
        )
        status = "ok" if not partial and policy.total >= policy.minimum_total else "partial"
        await self.repository.finish_hydration_run(
            hydration_run_id,
            status=status,
            platform_counts=dict(policy.counts),
            stop_reason=reason.value,
        )
        await self._emit(
            holder,
            "catalog_fetch_finished",
            phase="provider_search",
            status=status,
            total=policy.total,
            platform_counts=policy.counts,
            partial_platforms=sorted(partial),
            stop_reason=reason.value,
            message=f"已收集 {policy.total} 件有效候选",
        )
        return HydrationResult(
            status,
            policy.total,
            dict(policy.counts),
            sorted(partial),
            reason.value,
            status,
            tuple(sorted(hydrated_offer_ids)),
        )
