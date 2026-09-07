"""LangGraph BaseStore backed by plain-text PostgreSQL/pgvector memory."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextvars import ContextVar

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
from sqlalchemy import and_, func, not_, select

from app.auth.service import utc_naive
from app.config import Settings
from app.database.models import MemoryEmbedding, MemoryEntry, User
from app.database.session import Database
from app.memory.keywords import extract_keywords
from app.memory.service import MemoryService
from app.search.encoder import EmbeddingEncoder

_RRF_K = 60
_MEMORY_TEXT_VERSION = "memory-text-v2"
_recall_metrics: ContextVar[dict[str, int | str] | None] = ContextVar(
    "globuy_memory_recall_metrics", default=None
)


def memory_decay_factor(age_days: float, *, window_days: int = 180, floor: float = 0.6) -> float:
    age = min(max(0.0, age_days), float(window_days))
    return 1 - (1 - floor) * age / window_days


def current_memory_recall_metrics() -> dict[str, int | str]:
    return dict(_recall_metrics.get() or {})


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
        raise RuntimeError("Use asynchronous BaseStore methods from Agent code")

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

    async def _entry(self, user_id: str, memory_id: str) -> MemoryEntry | None:
        async with self.database.sessions() as session:
            return await session.scalar(
                select(MemoryEntry).where(
                    MemoryEntry.user_id == user_id,
                    MemoryEntry.memory_id == memory_id,
                )
            )

    @staticmethod
    def _item(entry: MemoryEntry) -> Item:
        return Item(
            namespace=("users", entry.user_id, "memories"),
            key=entry.memory_id,
            value={"memory_id": entry.memory_id, "memory": entry.memory, "source": entry.source},
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        )

    async def _get(self, op: GetOp) -> Item | None:
        entry = await self._entry(self._user(op.namespace), op.key)
        return self._item(entry) if entry else None

    async def _put(self, op: PutOp) -> None:
        user_id = self._user(op.namespace)
        entry = await self._entry(user_id, op.key)
        if op.value is None:
            if entry:
                await self.service.delete(user_id, entry.memory_id)
            return
        memory = str(op.value.get("memory") or op.value.get("content") or "").strip()
        if entry is None:
            await self.service.create(user_id, memory=memory, source="agent")
        else:
            await self.service.update(user_id, entry.memory_id, memory=memory)

    async def _vector_lane(
        self, user_id: str, vector: list[float], limit: int
    ) -> tuple[list[tuple[MemoryEntry, float]], int]:
        metadata = self.encoder.metadata
        async with self.database.sessions() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                distance = MemoryEmbedding.embedding.cosine_distance(vector)
                compatible = and_(
                    MemoryEmbedding.embedding_model == metadata.model_id,
                    MemoryEmbedding.embedding_revision == metadata.revision,
                    MemoryEmbedding.dimensions == metadata.dimensions,
                    MemoryEmbedding.normalized.is_(True),
                    MemoryEmbedding.semantic_text_version == _MEMORY_TEXT_VERSION,
                )
                rows = (
                    await session.execute(
                        select(MemoryEntry, (1 - distance).label("similarity"))
                        .join(MemoryEmbedding, MemoryEmbedding.memory_id == MemoryEntry.memory_id)
                        .where(
                            MemoryEntry.user_id == user_id,
                            compatible,
                        )
                        .order_by(distance)
                        .limit(limit)
                    )
                ).all()
                mismatches = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(MemoryEmbedding)
                        .join(MemoryEntry, MemoryEntry.memory_id == MemoryEmbedding.memory_id)
                        .where(
                            MemoryEntry.user_id == user_id,
                            not_(compatible),
                        )
                    )
                    or 0
                )
                return [(entry, max(0.0, float(score))) for entry, score in rows], mismatches
            rows = (
                await session.execute(
                    select(MemoryEntry, MemoryEmbedding)
                    .join(MemoryEmbedding, MemoryEmbedding.memory_id == MemoryEntry.memory_id)
                    .where(MemoryEntry.user_id == user_id)
                )
            ).all()
        scored: list[tuple[MemoryEntry, float]] = []
        mismatches = 0
        for entry, projection in rows:
            compatible = (
                projection.embedding_model == metadata.model_id
                and projection.embedding_revision == metadata.revision
                and projection.dimensions == metadata.dimensions
                and projection.normalized
                and projection.semantic_text_version == _MEMORY_TEXT_VERSION
            )
            if not compatible:
                mismatches += 1
                continue
            score = sum(a * b for a, b in zip(vector, projection.embedding, strict=False))
            scored.append((entry, max(0.0, score)))
        return sorted(scored, key=lambda pair: pair[1], reverse=True)[:limit], mismatches

    async def _keyword_lane(
        self, user_id: str, query_keywords: set[str], limit: int
    ) -> list[tuple[MemoryEntry, float]]:
        if not query_keywords:
            return []
        async with self.database.sessions() as session:
            statement = select(MemoryEntry).where(
                MemoryEntry.user_id == user_id
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.where(
                    MemoryEntry.keywords.op("&&")(sorted(query_keywords))
                ).limit(limit * 5)
            candidates = list((await session.scalars(statement)).all())
        scored = [
            (entry, float(len(query_keywords.intersection(entry.keywords or []))))
            for entry in candidates
            if query_keywords.intersection(entry.keywords or [])
        ]
        return sorted(scored, key=lambda pair: (pair[1], pair[0].updated_at), reverse=True)[:limit]

    async def _search_ranked(
        self,
        user_id: str,
        *,
        query: str | None,
        limit: int,
        offset: int,
        apply_decay: bool,
    ) -> list[SearchItem]:
        if not query:
            async with self.database.sessions() as session:
                entries = list(
                    (
                        await session.scalars(
                            select(MemoryEntry)
                            .where(MemoryEntry.user_id == user_id)
                            .order_by(MemoryEntry.updated_at.desc())
                            .offset(offset)
                            .limit(limit)
                        )
                    ).all()
                )
            return [self._search_item(entry, None) for entry in entries]

        requested = limit + offset
        pool = max(self.settings.memory_recall_candidate_pool, requested * 5)
        vector_lane, mismatches = await self._vector_lane(
            user_id, self.encoder.encode_query(query), pool
        )
        keyword_lane = await self._keyword_lane(
            user_id, set(extract_keywords(query)), pool
        )
        fused: dict[str, tuple[MemoryEntry, float]] = {}
        for lane in (vector_lane, keyword_lane):
            for rank, (entry, _score) in enumerate(lane, start=1):
                previous = fused.get(entry.memory_id, (entry, 0.0))[1]
                fused[entry.memory_id] = (entry, previous + 1 / (_RRF_K + rank))
        scored = list(fused.values())
        if apply_decay:
            now = utc_naive()
            window = self.settings.memory_decay_window_days
            floor = self.settings.memory_decay_floor
            scored = [
                (
                    entry,
                    score * memory_decay_factor(
                        (now - entry.last_confirmed_at).total_seconds() / 86400,
                        window_days=window,
                        floor=floor,
                    ),
                )
                for entry, score in scored
            ]
        ranked = sorted(
            scored,
            key=lambda pair: (
                pair[1],
                pair[0].last_confirmed_at,
                pair[0].updated_at,
                pair[0].memory_id,
            ),
            reverse=True,
        )[offset : offset + limit]
        _recall_metrics.set(
            {
                "vector_hits": len(vector_lane),
                "keyword_hits": len(keyword_lane),
                "fused_count": len(fused),
                "final_count": len(ranked),
                "vector_metadata_mismatches": mismatches,
                **({"degraded_reason": "vector_metadata_mismatch"} if mismatches else {}),
            }
        )
        return [self._search_item(entry, score) for entry, score in ranked]

    async def _search(self, op: SearchOp) -> list[SearchItem]:
        return await self._search_ranked(
            self._user(op.namespace_prefix),
            query=op.query,
            limit=op.limit,
            offset=op.offset,
            apply_decay=True,
        )

    async def asearch_for_consolidation(
        self, user_id: str, *, query: str, limit: int = 5
    ) -> list[SearchItem]:
        return await self._search_ranked(
            user_id,
            query=query,
            limit=limit,
            offset=0,
            apply_decay=False,
        )

    def _search_item(self, entry: MemoryEntry, score: float | None) -> SearchItem:
        return SearchItem(
            namespace=("users", entry.user_id, "memories"),
            key=entry.memory_id,
            value=self._item(entry).value,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            score=score,
        )

    async def _namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        async with self.database.sessions() as session:
            ids = list((await session.scalars(select(User.user_id).order_by(User.user_id))).all())
        return [("users", user_id, "memories") for user_id in ids][op.offset : op.offset + op.limit]


__all__ = ["PostgresMemoryStore", "current_memory_recall_metrics"]
