"""Publish durable memory outbox events to the dedicated OpenSearch index."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import get_settings
from app.database.models import OutboxEvent
from app.database.session import Database
from app.infrastructure.opensearch import build_opensearch_client
from app.memory.opensearch_store import memory_index_body
from app.search.encoder import get_embedding_encoder


class MemoryOutboxWorker:
    def __init__(self, database: Database, batch_size: int = 100) -> None:
        self.database = database
        self.settings = get_settings()
        self.client = build_opensearch_client(self.settings)
        self.encoder = get_embedding_encoder()
        self.batch_size = batch_size

    async def ensure_index(self) -> None:
        exists = await asyncio.to_thread(
            self.client.indices.exists, index=self.settings.opensearch_memory_index
        )
        if not exists:
            await asyncio.to_thread(
                self.client.indices.create,
                index=self.settings.opensearch_memory_index,
                body=memory_index_body(self.encoder.metadata.dimensions),
            )
        else:
            await asyncio.to_thread(
                self.client.indices.put_mapping,
                index=self.settings.opensearch_memory_index,
                body={"properties": {"skill_id": {"type": "keyword"}}},
            )

    async def run_once(self) -> dict[str, int]:
        await self.ensure_index()
        async with self.database.sessions() as session:
            events = list(
                (
                    await session.scalars(
                        select(OutboxEvent)
                        .where(
                            OutboxEvent.aggregate_type == "memory",
                            OutboxEvent.published_at.is_(None),
                        )
                        .order_by(OutboxEvent.created_at)
                        .limit(self.batch_size)
                    )
                ).all()
            )
        published = 0
        failed = 0
        for event in events:
            try:
                payload = event.payload_json
                if event.event_type == "memory.deleted":
                    await asyncio.to_thread(
                        self.client.delete,
                        index=self.settings.opensearch_memory_index,
                        id=event.aggregate_id,
                        ignore=[404],
                    )
                else:
                    vector = self.encoder.encode_documents([str(payload["content"])])[0]
                    document = {**payload, "content_vector": vector}
                    await asyncio.to_thread(
                        self.client.index,
                        index=self.settings.opensearch_memory_index,
                        id=event.aggregate_id,
                        body=document,
                        refresh=True,
                    )
                async with self.database.sessions.begin() as session:
                    current = await session.get(
                        OutboxEvent, event.event_id, with_for_update=True
                    )
                    if current is not None:
                        current.published_at = datetime.now(UTC).replace(tzinfo=None)
                        current.attempts += 1
                        current.last_error_code = None
                published += 1
            except Exception:
                async with self.database.sessions.begin() as session:
                    current = await session.get(
                        OutboxEvent, event.event_id, with_for_update=True
                    )
                    if current is not None:
                        current.attempts += 1
                        current.last_error_code = "opensearch_publish_failed"
                failed += 1
        return {"claimed": len(events), "published": published, "failed": failed}


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
    worker = MemoryOutboxWorker(database)
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
