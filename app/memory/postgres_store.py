"""LangGraph BaseStore backed by PostgreSQL truth and pgvector recall."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)
from sqlalchemy import select

from app.config import Settings
from app.database.models import MemoryEmbedding, MemoryEntry, User
from app.database.services import MemoryService
from app.database.session import Database
from app.memory.keywords import extract_keywords
from app.search.encoder import EmbeddingEncoder

_RRF_K = 60
_MEMORY_TEXT_VERSION = "memory-content-v1"


class PostgresMemoryStore(BaseStore):
    """BaseStore namespace contract: ("users", user_id, "memories")."""

    def __init__(
        self,
        database: Database,
        service: MemoryService,
        encoder: EmbeddingEncoder,
        settings: Settings,
    ) -> None:
        self.database = database
        self.service = service
        self.encoder = encoder
        self.settings = settings

    @staticmethod
    def _user(namespace: tuple[str, ...]) -> str:
        if len(namespace) < 3 or namespace[0] != "users" or namespace[2] != "memories":
            raise ValueError('memory namespace must be ("users", user_id, "memories")')
        return namespace[1]

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.abatch(ops))
        raise RuntimeError("Use abatch/aget/asearch/aput from asynchronous Agent code")

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        results: list[Result] = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(await self._get(op))
            elif isinstance(op, PutOp):
                await self._put(op)
                results.append(None)
            elif isinstance(op, SearchOp):
                results.append(await self._search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(await self._namespaces(op))
            else:
                raise NotImplementedError(type(op).__name__)
        return results

    async def _entry_by_key(self, user_id: str, key: str) -> MemoryEntry | None:
        async with self.database.sessions() as session:
            return await session.scalar(
                select(MemoryEntry).where(
                    MemoryEntry.user_id == user_id,
                    MemoryEntry.key == key,
                    MemoryEntry.status == "active",
                    MemoryEntry.lifecycle_status == "active",
                )
            )

    @staticmethod
    def _item(entry: MemoryEntry) -> Item:
        return Item(
            namespace=("users", entry.user_id, "memories"),
            key=entry.key,
            value={
                "memory_id": entry.memory_id,
                "category": entry.category,
                "content": entry.content,
                "confidence": float(entry.confidence),
                "source": entry.source,
                "keywords": list(entry.keywords or []),
                "lifecycle_status": entry.lifecycle_status,
            },
            created_at=entry.created_at.replace(tzinfo=UTC),
            updated_at=entry.updated_at.replace(tzinfo=UTC),
        )

    async def _get(self, op: GetOp) -> Item | None:
        entry = await self._entry_by_key(self._user(op.namespace), op.key)
        return self._item(entry) if entry else None

    async def _put(self, op: PutOp) -> None:
        user_id = self._user(op.namespace)
        entry = await self._entry_by_key(user_id, op.key)
        if op.value is None:
            if entry:
                await self.service.delete(user_id, entry.memory_id)
            return
        if not bool(op.value.get("confirmed_by_user")):
            raise PermissionError("long-term memory writes require explicit user confirmation")
        category = str(op.value.get("category") or "preference")
        content = str(op.value.get("content") or op.value.get("memory") or "").strip()
        confidence = Decimal(str(op.value.get("confidence", 1)))
        if entry is None:
            await self.service.create(
                user_id,
                category=category,
                key=op.key,
                content=content,
                confidence=confidence,
                source_thread_id=op.value.get("source_thread_id"),
                source_run_id=op.value.get("source_run_id"),
            )
        else:
            await self.service.update(
                user_id,
                entry.memory_id,
                category=category,
                content=content,
                confidence=confidence,
            )

    def _decay(self, entry: MemoryEntry, now: datetime) -> float:
        if entry.category == "blacklist":
            return 1.0
        half_life = (
            self.settings.memory_preference_half_life_days
            if entry.category == "preference"
            else self.settings.memory_history_half_life_days
        )
        anchor = entry.last_reinforced_at or entry.updated_at
        age_days = max(0.0, (now - anchor).total_seconds() / 86400)
        return math.pow(2.0, -age_days / half_life)

    async def _vector_lane(
        self, user_id: str, vector: list[float], limit: int
    ) -> list[tuple[MemoryEntry, float]]:
        metadata = self.encoder.metadata
        async with self.database.sessions() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                distance = MemoryEmbedding.embedding.cosine_distance(vector)
                rows = (
                    await session.execute(
                        select(MemoryEntry, (1 - distance).label("similarity"))
                        .join(MemoryEmbedding, MemoryEmbedding.memory_id == MemoryEntry.memory_id)
                        .where(
                            MemoryEntry.user_id == user_id,
                            MemoryEntry.status == "active",
                            MemoryEntry.lifecycle_status == "active",
                            MemoryEntry.category != "blacklist",
                            MemoryEmbedding.embedding_model == metadata.model_id,
                            MemoryEmbedding.embedding_revision == metadata.revision,
                            MemoryEmbedding.dimensions == metadata.dimensions,
                            MemoryEmbedding.normalized.is_(True),
                            MemoryEmbedding.semantic_text_version == _MEMORY_TEXT_VERSION,
                        )
                        .order_by(distance)
                        .limit(limit)
                    )
                ).all()
                return [(entry, max(0.0, float(score))) for entry, score in rows]

            rows = (
                await session.execute(
                    select(MemoryEntry, MemoryEmbedding)
                    .join(MemoryEmbedding, MemoryEmbedding.memory_id == MemoryEntry.memory_id)
                    .where(
                        MemoryEntry.user_id == user_id,
                        MemoryEntry.status == "active",
                        MemoryEntry.lifecycle_status == "active",
                        MemoryEntry.category != "blacklist",
                    )
                )
            ).all()
        scored: list[tuple[MemoryEntry, float]] = []
        for entry, embedding in rows:
            dot = sum(a * b for a, b in zip(vector, embedding.embedding, strict=False))
            scored.append((entry, max(0.0, dot)))
        return sorted(scored, key=lambda pair: pair[1], reverse=True)[:limit]

    async def _keyword_lane(
        self,
        user_id: str,
        query_keywords: set[str],
        active: list[MemoryEntry],
        limit: int,
    ) -> list[tuple[MemoryEntry, float]]:
        if not query_keywords:
            return []
        async with self.database.sessions() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                candidates = list(
                    (
                        await session.scalars(
                            select(MemoryEntry)
                            .where(
                                MemoryEntry.user_id == user_id,
                                MemoryEntry.status == "active",
                                MemoryEntry.lifecycle_status == "active",
                                MemoryEntry.category != "blacklist",
                                MemoryEntry.keywords.op("&&")(sorted(query_keywords)),
                            )
                            .limit(limit * 5)
                        )
                    ).all()
                )
            else:
                candidates = [entry for entry in active if entry.category != "blacklist"]
        scored = [
            (entry, float(len(query_keywords.intersection(entry.keywords or []))))
            for entry in candidates
            if query_keywords.intersection(entry.keywords or [])
        ]
        return sorted(
            scored,
            key=lambda pair: (pair[1], pair[0].updated_at),
            reverse=True,
        )[:limit]

    async def _search(self, op: SearchOp) -> list[SearchItem]:
        user_id = self._user(op.namespace_prefix)
        limit = op.limit + op.offset
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self.database.sessions() as session:
            active = list(
                (
                    await session.scalars(
                        select(MemoryEntry).where(
                            MemoryEntry.user_id == user_id,
                            MemoryEntry.status == "active",
                            MemoryEntry.lifecycle_status == "active",
                        )
                    )
                ).all()
            )
        hard_rules = [entry for entry in active if entry.category == "blacklist"]
        if not op.query:
            ordered = sorted(
                active,
                key=lambda item: (item.category != "blacklist", -item.updated_at.timestamp()),
            )[op.offset : op.offset + op.limit]
            return [self._search_item(item, None) for item in ordered]

        pool = max(self.settings.memory_recall_candidate_pool, limit * 5)
        vector = self.encoder.encode_query(op.query)
        vector_lane = await self._vector_lane(user_id, vector, pool)
        query_keywords = set(extract_keywords(op.query))
        keyword_lane = await self._keyword_lane(user_id, query_keywords, active, pool)

        fused: dict[str, tuple[MemoryEntry, float]] = {}
        for lane in (vector_lane, keyword_lane):
            for rank, (entry, _lane_score) in enumerate(lane, start=1):
                previous = fused.get(entry.memory_id, (entry, 0.0))[1]
                fused[entry.memory_id] = (entry, previous + 1 / (_RRF_K + rank))
        ranked = sorted(
            (
                (entry, score * float(entry.confidence) * self._decay(entry, now))
                for entry, score in fused.values()
            ),
            key=lambda pair: (pair[1], pair[0].updated_at),
            reverse=True,
        )
        hard = [
            (entry, None) for entry in sorted(hard_rules, key=lambda x: x.updated_at, reverse=True)
        ]
        selected = (hard + ranked)[op.offset : op.offset + op.limit]
        return [self._search_item(entry, score) for entry, score in selected]

    def _search_item(self, entry: MemoryEntry, score: float | None) -> SearchItem:
        return SearchItem(
            namespace=("users", entry.user_id, "memories"),
            key=entry.key,
            value=self._item(entry).value,
            created_at=entry.created_at.replace(tzinfo=UTC),
            updated_at=entry.updated_at.replace(tzinfo=UTC),
            score=score,
        )

    async def _namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        async with self.database.sessions() as session:
            user_ids = list(
                (await session.scalars(select(User.user_id).order_by(User.user_id))).all()
            )
        namespaces = [("users", user_id, "memories") for user_id in user_ids]
        return namespaces[op.offset : op.offset + op.limit]
