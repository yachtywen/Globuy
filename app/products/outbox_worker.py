"""Publish MySQL product changes to the versioned OpenSearch product index."""

from __future__ import annotations

import argparse
import asyncio

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

    async def run_once(self) -> dict[str, int]:
        await asyncio.to_thread(self.manager.ensure_pipeline)
        await asyncio.to_thread(self.manager.ensure_index)
        async with self.database.sessions() as session:
            events = list(
                (
                    await session.scalars(
                        select(OutboxEvent)
                        .where(
                            OutboxEvent.aggregate_type == "product",
                            OutboxEvent.published_at.is_(None),
                        )
                        .order_by(OutboxEvent.created_at, OutboxEvent.event_id)
                        .limit(self.batch_size)
                    )
                ).all()
            )
        published = 0
        failed = 0
        for event in events:
            try:
                offer_id = str(event.payload_json["offer_id"])
                async with self.database.sessions() as session:
                    row = (
                        await session.execute(
                            select(Product, Offer)
                            .join(Offer, Offer.product_id == Product.product_id)
                            .where(Offer.offer_id == offer_id)
                        )
                    ).one_or_none()
                if (
                    row is not None
                    and row[0].status == "active"
                    and row[1].is_active
                    and row[1].current_price is not None
                ):
                    await asyncio.to_thread(
                        self.manager.index_items, [catalog_item(row[0], row[1])]
                    )
                else:
                    await asyncio.to_thread(
                        self.manager.client.delete,
                        index=self.settings.opensearch_product_index,
                        id=str(event.payload_json["item_id"]),
                        ignore=[404],
                        refresh=True,
                    )
                await self._complete(event.event_id, None)
                published += 1
            except Exception:
                await self._complete(event.event_id, "opensearch_publish_failed")
                failed += 1
        return {"claimed": len(events), "published": published, "failed": failed}

    async def _complete(self, event_id: str, error_code: str | None) -> None:
        async with self.database.sessions.begin() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if event is None:
                return
            event.attempts += 1
            event.last_error_code = error_code
            if error_code is None:
                event.published_at = utc_naive()


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
