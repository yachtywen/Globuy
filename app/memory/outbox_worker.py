"""Build pgvector memory projections and enforce the bounded memory lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, select

from app.auth.service import utc_naive
from app.config import Settings, get_settings
from app.database.models import (
    MemoryCandidate,
    MemoryEmbedding,
    MemoryEntry,
    MemoryVersion,
    OutboxEvent,
)
from app.database.session import Database
from app.search.encoder import EmbeddingEncoder, get_embedding_encoder

_MEMORY_TEXT_VERSION = "memory-content-v1"


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
                await self._publish(event.event_id)
                published += 1
            except Exception:
                async with self.database.sessions.begin() as session:
                    current = await session.get(OutboxEvent, event.event_id, with_for_update=True)
                    if current is not None:
                        current.attempts += 1
                        current.last_error_code = "pgvector_projection_failed"
                failed += 1
        lifecycle = await self.run_lifecycle_once()
        return {
            "claimed": len(events),
            "published": published,
            "failed": failed,
            **lifecycle,
        }

    async def _publish(self, event_id: str) -> None:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if event is None or event.published_at is not None:
                return
            if event.event_type == "memory.deleted":
                await session.execute(
                    delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == event.aggregate_id)
                )
            else:
                entry = await session.get(MemoryEntry, event.aggregate_id)
                if entry is None or entry.lifecycle_status != "active" or entry.status != "active":
                    await session.execute(
                        delete(MemoryEmbedding).where(
                            MemoryEmbedding.memory_id == event.aggregate_id
                        )
                    )
                else:
                    vector = self.encoder.encode_documents([entry.content])[0]
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
                    projection.content_hash = hashlib.sha256(entry.content.encode()).hexdigest()
                    projection.embedded_at = now
            event.published_at = now
            event.attempts += 1
            event.last_error_code = None

    async def run_lifecycle_once(self) -> dict[str, int]:
        now = utc_naive()
        expired_candidates = 0
        archived = 0
        purged = 0
        async with self.database.sessions.begin() as session:
            pending = list(
                (
                    await session.scalars(
                        select(MemoryCandidate).where(
                            MemoryCandidate.status == "pending",
                            MemoryCandidate.expires_at <= now,
                        )
                    )
                ).all()
            )
            for candidate in pending:
                candidate.status = "expired"
                candidate.decided_at = now
            expired_candidates = len(pending)
            await session.execute(
                delete(MemoryCandidate).where(
                    MemoryCandidate.status.in_(["expired", "rejected"]),
                    MemoryCandidate.decided_at <= now - timedelta(days=90),
                )
            )

            entries = list(
                (
                    await session.scalars(
                        select(MemoryEntry).where(
                            MemoryEntry.status == "active",
                            MemoryEntry.lifecycle_status == "active",
                            MemoryEntry.category.in_(["preference", "history"]),
                        )
                    )
                ).all()
            )
            for entry in entries:
                is_preference = entry.category == "preference"
                half_life = (
                    self.settings.memory_preference_half_life_days
                    if is_preference
                    else self.settings.memory_history_half_life_days
                )
                archive_days = (
                    self.settings.memory_preference_archive_days
                    if is_preference
                    else self.settings.memory_history_archive_days
                )
                threshold = 0.10 if is_preference else 0.05
                age = (now - entry.last_reinforced_at).total_seconds() / 86400
                effective = float(entry.confidence) * math.pow(2.0, -max(age, 0) / half_life)
                if age < archive_days or effective > threshold:
                    continue
                entry.lifecycle_status = "archived"
                entry.archived_at = now
                entry.purge_after = now + timedelta(days=730 if is_preference else 365)
                entry.updated_at = now
                entry.version += 1
                session.add(
                    MemoryVersion(
                        memory_version_id=uuid4().hex,
                        memory_id=entry.memory_id,
                        version=entry.version,
                        operation="archive",
                        snapshot_json={
                            "memory_id": entry.memory_id,
                            "category": entry.category,
                            "key": entry.key,
                            "lifecycle_status": "archived",
                        },
                        created_at=now,
                    )
                )
                await session.execute(
                    delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == entry.memory_id)
                )
                archived += 1

            due_for_purge = list(
                (
                    await session.scalars(
                        select(MemoryEntry.memory_id).where(
                            MemoryEntry.lifecycle_status == "archived",
                            MemoryEntry.purge_after.is_not(None),
                            MemoryEntry.purge_after <= now,
                        )
                    )
                ).all()
            )
            if due_for_purge:
                result = await session.execute(
                    delete(MemoryEntry).where(MemoryEntry.memory_id.in_(due_for_purge))
                )
                purged = int(result.rowcount or 0)
        return {
            "expired_candidates": expired_candidates,
            "archived": archived,
            "purged": purged,
        }


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
    worker = MemoryOutboxWorker(database, settings=settings)
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
