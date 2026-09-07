"""Project plain-text memory Outbox events into pgvector."""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, or_, select

from app.auth.service import utc_naive
from app.config import Settings, get_settings
from app.database.models import MemoryEmbedding, MemoryEntry, OutboxEvent
from app.database.session import Database
from app.memory.service import memory_hash
from app.search.encoder import EmbeddingEncoder, get_embedding_encoder

_MEMORY_TEXT_VERSION = "memory-text-v2"
_RETRY_SECONDS = (5, 10, 20, 40, 80, 160, 320, 600)


class MemoryOutboxWorker:
    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        encoder: EmbeddingEncoder | None = None,
        batch_size: int = 100,
    ) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.encoder = encoder or get_embedding_encoder()
        self.batch_size = batch_size

    async def run_once(self) -> dict[str, int]:
        claims = await self._claim()
        published = 0
        failed = 0
        for event_id, token in claims:
            try:
                await self._publish(event_id, token)
                published += 1
            except Exception:  # noqa: BLE001 - durable retry state records the failure
                async with self.database.sessions.begin() as session:
                    event = await session.get(OutboxEvent, event_id, with_for_update=True)
                    if event is not None and event.claim_token == token:
                        event.attempts += 1
                        event.last_error_code = "pgvector_projection_failed"
                        event.claimed_at = None
                        event.claim_token = None
                        if event.attempts > self.settings.memory_outbox_max_attempts:
                            event.dead_lettered_at = utc_naive()
                            event.available_at = None
                        else:
                            delay = _RETRY_SECONDS[
                                min(event.attempts, len(_RETRY_SECONDS)) - 1
                            ]
                            event.available_at = utc_naive() + timedelta(seconds=delay)
                failed += 1
        return {"claimed": len(claims), "published": published, "failed": failed}

    async def _claim(self) -> list[tuple[str, str]]:
        now = utc_naive()
        expired = now - timedelta(seconds=self.settings.memory_outbox_lease_seconds)
        claims: list[tuple[str, str]] = []
        async with self.database.sessions.begin() as session:
            events = list(
                (
                    await session.scalars(
                        select(OutboxEvent)
                        .where(
                            OutboxEvent.aggregate_type == "memory",
                            OutboxEvent.published_at.is_(None),
                            OutboxEvent.dead_lettered_at.is_(None),
                            or_(
                                OutboxEvent.available_at.is_(None),
                                OutboxEvent.available_at <= now,
                            ),
                            or_(OutboxEvent.claimed_at.is_(None), OutboxEvent.claimed_at < expired),
                        )
                        .order_by(OutboxEvent.created_at)
                        .limit(self.batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for event in events:
                token = uuid4().hex
                event.claimed_at = now
                event.claim_token = token
                claims.append((event.event_id, token))
        return claims

    async def _publish(self, event_id: str, token: str) -> None:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if (
                event is None
                or event.published_at is not None
                or event.claim_token != token
            ):
                return
            entry = await session.get(MemoryEntry, event.aggregate_id)
            if event.event_type == "memory.deleted" or entry is None:
                await session.execute(
                    delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == event.aggregate_id)
                )
            else:
                vector = self.encoder.encode_documents([entry.memory])[0]
                metadata = self.encoder.metadata
                projection = await session.get(
                    MemoryEmbedding, entry.memory_id, with_for_update=True
                )
                if projection is None:
                    projection = MemoryEmbedding(memory_id=entry.memory_id)
                    session.add(projection)
                projection.embedding = vector
                projection.embedding_model = metadata.model_id
                projection.embedding_revision = metadata.revision
                projection.dimensions = metadata.dimensions
                projection.normalized = metadata.normalized
                projection.semantic_text_version = _MEMORY_TEXT_VERSION
                projection.content_hash = memory_hash(entry.memory)
                projection.embedded_at = now
            event.published_at = now
            event.attempts += 1
            event.last_error_code = None
            event.available_at = None
            event.claimed_at = None
            event.claim_token = None

    async def requeue(self, event_id: str) -> bool:
        async with self.database.sessions.begin() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if event is None or event.aggregate_type != "memory" or event.published_at is not None:
                return False
            event.attempts = 0
            event.last_error_code = None
            event.available_at = utc_naive()
            event.claimed_at = None
            event.claim_token = None
            event.dead_lettered_at = None
            return True


async def _main(serve: bool, poll_seconds: int, requeue_event: str | None) -> None:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("GLOBUY_DATABASE_URL is required")
    database = Database(
        settings.database_url.get_secret_value(),
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        pool_recycle=settings.database_pool_recycle_seconds,
    )
    worker = MemoryOutboxWorker(database, settings=settings)
    try:
        if requeue_event:
            print({"requeued": await worker.requeue(requeue_event)})
        elif serve:
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
    parser.add_argument("--requeue-event")
    args = parser.parse_args()
    asyncio.run(_main(args.serve, args.poll_seconds, args.requeue_event))


if __name__ == "__main__":
    main()
