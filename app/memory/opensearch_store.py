"""LangGraph BaseStore adapter backed by MySQL truth and a dedicated OpenSearch index."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC
from decimal import Decimal
from typing import Any

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
from opensearchpy import OpenSearch
from sqlalchemy import select

from app.database.models import MemoryEntry, User
from app.database.services import MemoryService
from app.database.session import Database
from app.search.encoder import EmbeddingEncoder


def memory_index_body(dimensions: int) -> dict[str, Any]:
    return {
        "settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}},
        "mappings": {
            "properties": {
                "memory_id": {"type": "keyword"},
                "user_id": {"type": "keyword"},
                "category": {"type": "keyword"},
                "key": {"type": "keyword"},
                "content": {"type": "text", "analyzer": "cjk"},
                "confidence": {"type": "float"},
                "status": {"type": "keyword"},
                "version": {"type": "integer"},
                "updated_at": {"type": "date"},
                "content_vector": {
                    "type": "knn_vector",
                    "dimension": dimensions,
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                    },
                },
            }
        },
    }


class GlobuyMemoryStore(BaseStore):
    """BaseStore namespace contract: ("users", user_id, "memories")."""

    def __init__(
        self,
        database: Database,
        service: MemoryService,
        client: OpenSearch,
        encoder: EmbeddingEncoder,
        index: str,
    ) -> None:
        self.database = database
        self.service = service
        self.client = client
        self.encoder = encoder
        self.index = index

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

    async def _search(self, op: SearchOp) -> list[SearchItem]:
        user_id = self._user(op.namespace_prefix)
        async with self.database.sessions() as session:
            hard_rules = list(
                (
                    await session.scalars(
                        select(MemoryEntry).where(
                            MemoryEntry.user_id == user_id,
                            MemoryEntry.status == "active",
                            MemoryEntry.category == "blacklist",
                        )
                    )
                ).all()
            )
        entries: dict[str, tuple[MemoryEntry, float | None]] = {
            item.memory_id: (item, None) for item in hard_rules
        }
        if op.query:
            try:
                vector = self.encoder.encode_query(op.query)
                body = {
                    "size": op.limit + op.offset,
                    "query": {
                        "knn": {
                            "content_vector": {
                                "vector": vector,
                                "k": op.limit + op.offset,
                                "filter": {
                                    "bool": {
                                        "filter": [
                                            {"term": {"user_id": user_id}},
                                            {"term": {"status": "active"}},
                                        ]
                                    }
                                },
                            }
                        }
                    },
                }
                response = await asyncio.to_thread(
                    self.client.search, index=self.index, body=body
                )
                ids_scores = [
                    (str(hit["_source"]["memory_id"]), float(hit.get("_score") or 0))
                    for hit in response.get("hits", {}).get("hits", [])
                ]
            except Exception:
                # MySQL hard rules remain effective even while semantic recall is unavailable.
                ids_scores = []
            if ids_scores:
                async with self.database.sessions() as session:
                    found = list(
                        (
                            await session.scalars(
                                select(MemoryEntry).where(
                                    MemoryEntry.memory_id.in_([item[0] for item in ids_scores]),
                                    MemoryEntry.status == "active",
                                )
                            )
                        ).all()
                    )
                score_map = dict(ids_scores)
                for item in found:
                    entries[item.memory_id] = (item, score_map.get(item.memory_id))
        else:
            async with self.database.sessions() as session:
                found = list(
                    (
                        await session.scalars(
                            select(MemoryEntry)
                            .where(MemoryEntry.user_id == user_id, MemoryEntry.status == "active")
                            .order_by(MemoryEntry.updated_at.desc())
                            .limit(op.limit + op.offset)
                        )
                    ).all()
                )
            entries.update({item.memory_id: (item, None) for item in found})
        ordered = sorted(
            entries.values(),
            key=lambda pair: (
                pair[0].category != "blacklist",
                -(pair[1] or 0),
                -pair[0].updated_at.timestamp(),
            ),
        )[op.offset : op.offset + op.limit]
        return [
            SearchItem(
                namespace=("users", item.user_id, "memories"),
                key=item.key,
                value=self._item(item).value,
                created_at=item.created_at.replace(tzinfo=UTC),
                updated_at=item.updated_at.replace(tzinfo=UTC),
                score=score,
            )
            for item, score in ordered
        ]

    async def _namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        async with self.database.sessions() as session:
            user_ids = list(
                (await session.scalars(select(User.user_id).order_by(User.user_id))).all()
            )
        namespaces = [("users", user_id, "memories") for user_id in user_ids]
        return namespaces[op.offset : op.offset + op.limit]
