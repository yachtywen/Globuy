"""Publish MySQL product changes to the versioned OpenSearch product index."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.auth.service import utc_naive
from app.config import get_settings
from app.database.models import Offer, OutboxEvent, Product
from app.database.session import Database
from app.infrastructure.opensearch import build_opensearch_client
from app.search.encoder import get_embedding_encoder
from app.search.service import ProductIndexManager, catalog_item


class ProductOutboxWorker:
    def __init__(self, database: Database, batch_size: int = 100) -> None:
        self.database = database
        self.settings = get_settings()
        self.manager = ProductIndexManager(
            build_opensearch_client(self.settings),
            get_embedding_encoder(),
            self.settings,
        )
        self.batch_size = batch_size

    async def run_once(self, preferred_offer_ids: tuple[str, ...] = ()) -> dict[str, int]:
        await asyncio.to_thread(self.manager.ensure_pipeline)
        await asyncio.to_thread(self.manager.ensure_index)
        now = utc_naive()
        claim_token = uuid4().hex
        async with self.database.sessions.begin() as session:
            statement = (
                select(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_type == "product",
                    OutboxEvent.published_at.is_(None),
                    OutboxEvent.attempts < self.settings.product_outbox_max_attempts,
                    (OutboxEvent.available_at.is_(None)) | (OutboxEvent.available_at <= now),
                    (OutboxEvent.claimed_at.is_(None))
                    | (OutboxEvent.claimed_at < now - timedelta(minutes=5)),
                )
            )
            if preferred_offer_ids:
                statement = statement.where(OutboxEvent.aggregate_id.in_(preferred_offer_ids))
            statement = (
                statement
                .order_by(OutboxEvent.created_at, OutboxEvent.event_id)
                .limit(
                    min(
                        1000,
                        max(self.batch_size, len(preferred_offer_ids) * 4),
                    )
                )
                .with_for_update(skip_locked=True)
            )
            events = list(
                (
                    await session.scalars(statement)
                ).all()
            )
            bounded: list[OutboxEvent] = []
            used_bytes = 0
            for event in events:
                event_bytes = len(
                    json.dumps(event.payload_json, ensure_ascii=False, default=str).encode("utf-8")
                )
                if bounded and used_bytes + event_bytes > self.settings.product_outbox_max_bytes:
                    break
                bounded.append(event)
                used_bytes += event_bytes
            events = bounded
            for event in events:
                event.claim_token = claim_token
                event.claimed_at = now
        if not events:
            return {
                "claimed": 0,
                "published": 0,
                "failed": 0,
                "embedded": 0,
                "reused_vectors": 0,
                "noops": 0,
            }
        grouped: dict[str, list[OutboxEvent]] = {}
        for event in events:
            oid = str(event.payload_json.get("offer_id") or event.aggregate_id)
            grouped.setdefault(oid, []).append(event)
        async with self.database.sessions() as session:
            rows = list(
                (
                    await session.execute(
                        select(Product, Offer)
                        .join(Offer, Offer.product_id == Product.product_id)
                        .where(Offer.offer_id.in_(list(grouped)))
                    )
                ).all()
            )
        active: list[dict] = []
        delete_ids = set(grouped)
        for product, offer in rows:
            if product.status == "active" and offer.is_active and offer.current_price is not None:
                active.append(catalog_item(product, offer))
                delete_ids.discard(offer.offer_id)
        try:
            result = await asyncio.to_thread(self.manager.project_items, active, sorted(delete_ids))
            failed_ids: set[str] = set()
            for error in result["errors"]:
                detail = next(iter(error.values()))
                if int(detail.get("status", 500)) != 404:
                    failed_ids.add(str(detail.get("_id")))
            async with self.database.sessions.begin() as session:
                for oid, related in grouped.items():
                    for original in related:
                        event = await session.get(
                            OutboxEvent, original.event_id, with_for_update=True
                        )
                        if event is None or event.claim_token != claim_token:
                            continue
                        event.attempts += 1
                        event.claimed_at = None
                        event.claim_token = None
                        if oid in failed_ids:
                            event.last_error_code = "opensearch_publish_failed"
                            event.available_at = utc_naive() + timedelta(
                                seconds=min(
                                    self.settings.product_outbox_retry_max_seconds,
                                    self.settings.product_outbox_retry_base_seconds
                                    * 2 ** min(event.attempts - 1, 8),
                                )
                            )
                        else:
                            event.last_error_code = None
                            event.published_at = utc_naive()
                    hashes = result["hashes"].get(oid)
                    if hashes and oid not in failed_ids:
                        offer = await session.get(Offer, oid)
                        if offer is not None:
                            product = await session.get(Product, offer.product_id)
                            if product is not None:
                                product.semantic_hash = hashes[0]
                            offer.projection_hash = hashes[1]
                            offer.projected_at = utc_naive()
            published = sum(len(value) for oid, value in grouped.items() if oid not in failed_ids)
            failed = len(events) - published
            return {
                "claimed": len(events),
                "published": published,
                "failed": failed,
                "embedded": result["embedded"],
                "reused_vectors": result["reused_vectors"],
                "noops": len(result["noops"]),
            }
        except Exception:
            for event in events:
                await self._complete(event.event_id, "opensearch_publish_failed")
            return {
                "claimed": len(events),
                "published": 0,
                "failed": len(events),
                "embedded": 0,
                "reused_vectors": 0,
                "noops": 0,
            }

    async def _complete(self, event_id: str, error_code: str | None) -> None:
        async with self.database.sessions.begin() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if event is None:
                return
            event.attempts += 1
            event.last_error_code = error_code
            event.claimed_at = None
            event.claim_token = None
            if error_code is None:
                event.published_at = utc_naive()
            else:
                event.available_at = utc_naive() + timedelta(
                    seconds=min(
                        self.settings.product_outbox_retry_max_seconds,
                        self.settings.product_outbox_retry_base_seconds
                        * 2 ** min(event.attempts - 1, 8),
                    )
                )


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
    worker = ProductOutboxWorker(database)
    try:
        if serve:
            while True:
                await worker.run_once()
                await asyncio.sleep(poll_seconds)
        else:
            print(await worker.run_once())
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(_main(args.serve, args.poll_seconds))


if __name__ == "__main__":
    main()
