"""Persistent price refresh worker with provider adapters and truthful failures."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select

from app.auth.service import utc_naive
from app.config import get_settings
from app.database.models import (
    Offer,
    OfferObservation,
    OutboxEvent,
    PriceRefreshItem,
    PriceRefreshRun,
    SourceSnapshot,
    WishlistItem,
)
from app.database.session import Database
from app.products.identity import stable_id
from app.products.schedule import current_beijing_day_start, next_daily_refresh


@dataclass(frozen=True)
class ProductDetail:
    price: Decimal
    currency: str


class ProductProvider(Protocol):
    async def get_detail(self, platform: str, source_item_id: str) -> ProductDetail: ...


class ProviderNotConfigured(RuntimeError):
    pass


class ProviderRegistry:
    def __init__(self, providers: dict[str, ProductProvider] | None = None) -> None:
        self.providers = providers or {}

    async def get_detail(self, platform: str, source_item_id: str) -> ProductDetail:
        provider = self.providers.get(platform)
        if provider is None:
            raise ProviderNotConfigured(f"{platform} detail provider is not configured")
        return await provider.get_detail(platform, source_item_id)


class PriceRefreshWorker:
    def __init__(
        self,
        database: Database,
        providers: ProviderRegistry,
        *,
        refresh_hours: int = 24,
        refresh_local_hour: int = 3,
        batch_size: int = 100,
        claim_lease_minutes: int = 10,
    ) -> None:
        self.database = database
        self.providers = providers
        self.refresh_hours = refresh_hours
        self.refresh_local_hour = refresh_local_hour
        self.batch_size = batch_size
        self.claim_lease_minutes = claim_lease_minutes

    async def run_once(self) -> dict[str, int | str]:
        return await self._run(due_only=True)

    async def refresh_item(self, wishlist_item_id: str) -> dict[str, int | str]:
        return await self._run(due_only=False, wishlist_item_id=wishlist_item_id)

    async def _run(self, *, due_only: bool, wishlist_item_id: str | None = None) -> dict[str, int | str]:
        now = utc_naive()
        refresh_run_id = uuid4().hex
        async with self.database.sessions.begin() as session:
            session.add(
                PriceRefreshRun(
                    refresh_run_id=refresh_run_id,
                    status="running",
                    started_at=now,
                    claimed_count=0,
                    success_count=0,
                    failure_count=0,
                )
            )
        async with self.database.sessions.begin() as session:
            rows = list(
                (
                    await session.execute(
                        select(WishlistItem, Offer)
                        .join(Offer, Offer.offer_id == WishlistItem.offer_id)
                        .where(WishlistItem.status == "active")
                        .where(WishlistItem.wishlist_item_id == wishlist_item_id if wishlist_item_id else True)
                        .where(True if not due_only else (WishlistItem.next_check_at.is_(None)) | (WishlistItem.next_check_at <= now))
                        .order_by(WishlistItem.next_check_at, WishlistItem.wishlist_item_id)
                        .limit(self.batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for item, _ in rows:
                item.next_check_at = now + timedelta(minutes=self.claim_lease_minutes)
        success = 0
        failure = 0
        for item, offer in rows:
            started = utc_naive()
            status = "failed"
            error_code = None
            try:
                reused_observation_id = await self._reuse_today_observation(
                    item.wishlist_item_id, offer.offer_id, now
                )
                if reused_observation_id is not None:
                    status = "succeeded"
                    completed_observation_id = reused_observation_id
                    success += 1
                    raise _ObservationReused
                detail = await self.providers.get_detail(offer.platform, offer.source_item_id)
                if detail.price < 0:
                    raise ValueError("provider returned a negative price")
                async with self.database.sessions.begin() as session:
                    current_offer = await session.get(Offer, offer.offer_id, with_for_update=True)
                    current_item = await session.get(
                        WishlistItem, item.wishlist_item_id, with_for_update=True
                    )
                    if current_offer is not None and current_item is not None:
                        observed_at = utc_naive()
                        snapshot_id = stable_id(
                            "snapshot", f"price:{refresh_run_id}:{offer.offer_id}"
                        )
                        observation_id = stable_id(
                            "observation", f"{snapshot_id}:{offer.offer_id}"
                        )
                        session.add(
                            SourceSnapshot(
                                snapshot_id=snapshot_id,
                                provider="product_detail_adapter",
                                platform=offer.platform,
                                captured_at=observed_at,
                                request_key=offer.source_item_id,
                                status="complete",
                                raw_payload_path=None,
                                raw_payload_sha256=None,
                                metadata_json={"refresh_run_id": refresh_run_id},
                                created_at=observed_at,
                            )
                        )
                        session.add(
                            OfferObservation(
                                observation_id=observation_id,
                                offer_id=offer.offer_id,
                                snapshot_id=snapshot_id,
                                observed_at=observed_at,
                                provider_record_key=offer.source_item_id,
                                price=detail.price,
                                currency=detail.currency,
                                rating_value=None,
                                rating_scale=None,
                                sales_value=None,
                                sales_scope=None,
                                stock_status=None,
                                raw_fields_json={"source_kind": "product_detail"},
                            )
                        )
                        current_offer.current_price = detail.price
                        current_offer.currency = detail.currency
                        current_offer.last_seen_at = observed_at
                        current_offer.last_observation_id = observation_id
                        current_item.last_checked_at = current_offer.last_seen_at
                        current_item.latest_observation_id = observation_id
                        current_item.failure_count = 0
                        current_item.last_error_code = None
                        current_item.next_check_at = next_daily_refresh(
                            observed_at, local_hour=self.refresh_local_hour
                        )
                        session.add(self._product_outbox(current_offer, observation_id))
                status = "succeeded"
                completed_observation_id = observation_id
                success += 1
            except _ObservationReused:
                pass
            except ProviderNotConfigured:
                error_code = "provider_unavailable"
                completed_observation_id = None
                failure += 1
            except Exception:
                error_code = "provider_error"
                completed_observation_id = None
                failure += 1
            if error_code:
                async with self.database.sessions.begin() as session:
                    current = await session.get(
                        WishlistItem, item.wishlist_item_id, with_for_update=True
                    )
                    if current is not None:
                        current.failure_count += 1
                        current.last_error_code = error_code
                        delay = min(168, self.refresh_hours * (2 ** min(current.failure_count, 3)))
                        current.next_check_at = utc_naive() + timedelta(hours=delay)
            elapsed = int((utc_naive() - started).total_seconds() * 1000)
            async with self.database.sessions.begin() as session:
                session.add(
                    PriceRefreshItem(
                        refresh_item_id=uuid4().hex,
                        refresh_run_id=refresh_run_id,
                        wishlist_item_id=item.wishlist_item_id,
                        offer_id=offer.offer_id,
                        status=status,
                        error_code=error_code,
                        duration_ms=elapsed,
                        observation_id=completed_observation_id,
                        created_at=utc_naive(),
                    )
                )
        async with self.database.sessions.begin() as session:
            run = await session.get(PriceRefreshRun, refresh_run_id, with_for_update=True)
            if run is not None:
                run.status = "succeeded" if failure == 0 else "partial"
                run.finished_at = utc_naive()
                run.claimed_count = len(rows)
                run.success_count = success
                run.failure_count = failure
        return {
            "refresh_run_id": refresh_run_id,
            "claimed": len(rows),
            "succeeded": success,
            "failed": failure,
        }

    async def _reuse_today_observation(
        self, wishlist_item_id: str, offer_id: str, now: datetime
    ) -> str | None:
        day_start = current_beijing_day_start(now)
        async with self.database.sessions.begin() as session:
            observation = await session.scalar(
                select(OfferObservation)
                .join(
                    SourceSnapshot,
                    SourceSnapshot.snapshot_id == OfferObservation.snapshot_id,
                )
                .where(
                    OfferObservation.offer_id == offer_id,
                    OfferObservation.observed_at >= day_start,
                    OfferObservation.price.is_not(None),
                    SourceSnapshot.provider != "offline_jsonl",
                )
                .order_by(OfferObservation.observed_at.desc())
                .limit(1)
            )
            if observation is None:
                return None
            current_offer = await session.get(Offer, offer_id, with_for_update=True)
            current_item = await session.get(
                WishlistItem, wishlist_item_id, with_for_update=True
            )
            if current_offer is None or current_item is None:
                return None
            current_offer.current_price = observation.price
            current_offer.currency = observation.currency
            current_offer.last_seen_at = observation.observed_at
            current_offer.last_observation_id = observation.observation_id
            current_item.last_checked_at = observation.observed_at
            current_item.latest_observation_id = observation.observation_id
            current_item.failure_count = 0
            current_item.last_error_code = None
            current_item.next_check_at = next_daily_refresh(
                now, local_hour=self.refresh_local_hour
            )
            session.add(self._product_outbox(current_offer, observation.observation_id))
            return observation.observation_id

    @staticmethod
    def _product_outbox(offer: Offer, observation_id: str) -> OutboxEvent:
        return OutboxEvent(
            event_id=uuid4().hex,
            aggregate_type="product",
            aggregate_id=offer.product_id,
            event_type="product.upserted",
            aggregate_version=1,
            payload_json={
                "product_id": offer.product_id,
                "offer_id": offer.offer_id,
                "item_id": f"{offer.platform}:{offer.source_item_id}",
                "observation_id": observation_id,
            },
            created_at=utc_naive(),
            attempts=0,
        )


class _ObservationReused(Exception):
    """Internal control signal after a same-day observation was reused."""


async def _serve(worker: PriceRefreshWorker, poll_seconds: int) -> None:
    while True:
        await worker.run_once()
        await asyncio.sleep(poll_seconds)


async def _main(serve: bool, poll_seconds: int) -> None:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("GLOBUY_DATABASE_URL is required")
    database = Database(
        settings.database_url.get_secret_value(),
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        pool_recycle=settings.database_pool_recycle_seconds,
    )
    worker = PriceRefreshWorker(
        database,
        ProviderRegistry(),
        refresh_hours=settings.price_refresh_interval_hours,
        refresh_local_hour=settings.price_refresh_local_hour,
    )
    try:
        if serve:
            await _serve(worker, poll_seconds)
        else:
            print(await worker.run_once())
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=300)
    args = parser.parse_args()
    asyncio.run(_main(args.serve, args.poll_seconds))


if __name__ == "__main__":
    main()
