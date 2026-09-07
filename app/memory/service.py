"""Transactional plain-text long-term memory service."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from typing import Any, Literal, NotRequired, TypedDict
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.errors import ApiError
from app.auth.service import utc_naive
from app.database.models import MemoryEntry, MemoryHistory, OutboxEvent
from app.database.session import Database
from app.memory.keywords import extract_keywords

MemoryEvent = Literal["ADD", "UPDATE", "DELETE", "NONE"]


class MemoryAction(TypedDict):
    event: MemoryEvent
    memory: str
    memory_id: str | None
    keywords: NotRequired[list[str]]


def normalize_memory(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split())


def memory_hash(value: str) -> str:
    return hashlib.sha256(normalize_memory(value).casefold().encode("utf-8")).hexdigest()


def merge_keywords(memory: str, proposed: Sequence[str]) -> list[str]:
    merged = extract_keywords(memory, limit=32)
    for raw in proposed[:8]:
        value = normalize_memory(str(raw)).casefold()
        if not value or len(value) > 64 or value in merged:
            continue
        merged.append(value)
        if len(merged) >= 32:
            break
    return merged


def memory_snapshot(item: MemoryEntry) -> dict[str, Any]:
    return {
        "memory_id": item.memory_id,
        "memory": item.memory,
        "source": item.source,
        "version": item.version,
        "created_at": item.created_at.isoformat(timespec="milliseconds") + "Z",
        "updated_at": item.updated_at.isoformat(timespec="milliseconds") + "Z",
        "last_confirmed_at": item.last_confirmed_at.isoformat(timespec="milliseconds") + "Z",
    }


class MemoryService:
    def __init__(self, database: Database, settings: Any | None = None) -> None:
        self.database = database
        self.settings = settings

    async def list(self, user_id: str) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(MemoryEntry)
                        .where(MemoryEntry.user_id == user_id)
                        .order_by(MemoryEntry.updated_at.desc(), MemoryEntry.memory_id)
                    )
                ).all()
            )
        return [memory_snapshot(item) for item in rows]

    async def create(
        self,
        user_id: str,
        *,
        memory: str,
        source_thread_id: str | None = None,
        source_run_id: str | None = None,
        source: Literal["user", "agent", "import"] = "user",
    ) -> dict[str, Any]:
        value = normalize_memory(memory)
        if not value:
            raise ApiError(422, "MEMORY_EMPTY", "Memory text cannot be empty")
        digest = memory_hash(value)
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            existing = await session.scalar(
                select(MemoryEntry).where(
                    MemoryEntry.user_id == user_id,
                    MemoryEntry.content_hash == digest,
                )
            )
            if existing is not None:
                existing.last_confirmed_at = now
                return memory_snapshot(existing)
            item = MemoryEntry(
                memory_id=uuid4().hex,
                user_id=user_id,
                memory=value,
                content_hash=digest,
                keywords=merge_keywords(value, []),
                source=source,
                source_thread_id=source_thread_id,
                source_run_id=source_run_id,
                version=1,
                created_at=now,
                updated_at=now,
                last_confirmed_at=now,
            )
            session.add(item)
            self._record(session, item, "ADD", None, value, now)
            session.add(self._outbox(item, "memory.upserted", now))
        return memory_snapshot(item)

    async def update(
        self,
        user_id: str,
        memory_id: str,
        *,
        memory: str,
        source_thread_id: str | None = None,
        source_run_id: str | None = None,
    ) -> dict[str, Any]:
        changes = await self.apply_actions(
            user_id,
            [{"event": "UPDATE", "memory": memory, "memory_id": memory_id, "keywords": []}],
            source_thread_id=source_thread_id,
            source_run_id=source_run_id,
            source="user",
        )
        if not changes:
            async with self.database.sessions() as session:
                item = await session.get(MemoryEntry, memory_id)
                if item is None or item.user_id != user_id:
                    raise ApiError(404, "MEMORY_NOT_FOUND", "Memory does not exist")
                return memory_snapshot(item)
        return changes[0]["entry"]

    async def delete(
        self,
        user_id: str,
        memory_id: str,
        *,
        source_thread_id: str | None = None,
        source_run_id: str | None = None,
    ) -> None:
        await self.apply_actions(
            user_id,
            [{"event": "DELETE", "memory": "", "memory_id": memory_id, "keywords": []}],
            source_thread_id=source_thread_id,
            source_run_id=source_run_id,
            source="user",
        )

    async def history(self, user_id: str, memory_id: str) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(MemoryHistory)
                        .where(
                            MemoryHistory.memory_id == memory_id,
                            MemoryHistory.user_id == user_id,
                        )
                        .order_by(
                            MemoryHistory.created_at,
                            MemoryHistory.memory_version,
                            MemoryHistory.history_id,
                        )
                    )
                ).all()
            )
            if not rows:
                raise ApiError(404, "MEMORY_NOT_FOUND", "Memory does not exist")
        return [
            {
                "history_id": item.history_id,
                "memory_id": item.memory_id,
                "event": item.event,
                "memory_version": item.memory_version,
                "old_memory": item.old_memory,
                "new_memory": item.new_memory,
                "source_thread_id": item.source_thread_id,
                "source_run_id": item.source_run_id,
                "created_at": item.created_at.isoformat(timespec="milliseconds") + "Z",
            }
            for item in rows
        ]

    async def apply_actions(
        self,
        user_id: str,
        actions: Sequence[MemoryAction],
        *,
        source_thread_id: str | None,
        source_run_id: str | None,
        source: Literal["user", "agent", "import"] = "agent",
    ) -> list[dict[str, Any]]:
        target_ids = [
            str(action["memory_id"])
            for action in actions
            if action["event"] in {"UPDATE", "DELETE", "NONE"}
            and action.get("memory_id")
        ]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("a memory can be targeted at most once per action batch")
        now = utc_naive()
        changed: list[dict[str, Any]] = []
        async with self.database.sessions.begin() as session:
            existing: dict[str, MemoryEntry] = {}
            if target_ids:
                rows = list(
                    (
                        await session.scalars(
                            select(MemoryEntry)
                            .where(
                                MemoryEntry.memory_id.in_(target_ids),
                                MemoryEntry.user_id == user_id,
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                existing = {item.memory_id: item for item in rows}
                if set(target_ids) != set(existing):
                    raise ValueError("memory action referenced an unknown or cross-user id")

            for action in actions:
                event = action["event"]
                if event == "NONE":
                    if action.get("memory_id"):
                        item = existing[str(action["memory_id"])]
                        item.last_confirmed_at = now
                        changed.append(
                            {
                                "id": item.memory_id,
                                "event": "NONE",
                                "summary": item.memory,
                                "entry": memory_snapshot(item),
                            }
                        )
                    continue
                if event == "ADD":
                    value = normalize_memory(action["memory"])
                    if not value:
                        raise ValueError("ADD requires non-empty memory")
                    digest = memory_hash(value)
                    duplicate = await session.scalar(
                        select(MemoryEntry).where(
                            MemoryEntry.user_id == user_id,
                            MemoryEntry.content_hash == digest,
                        ).with_for_update()
                    )
                    if duplicate is not None:
                        duplicate.last_confirmed_at = now
                        changed.append(
                            {
                                "id": duplicate.memory_id,
                                "event": "NONE",
                                "summary": duplicate.memory,
                                "entry": memory_snapshot(duplicate),
                            }
                        )
                        continue
                    item = MemoryEntry(
                        memory_id=uuid4().hex,
                        user_id=user_id,
                        memory=value,
                        content_hash=digest,
                        keywords=merge_keywords(value, action.get("keywords", [])),
                        source=source,
                        source_thread_id=source_thread_id,
                        source_run_id=source_run_id,
                        version=1,
                        created_at=now,
                        updated_at=now,
                        last_confirmed_at=now,
                    )
                    try:
                        async with session.begin_nested():
                            session.add(item)
                            await session.flush([item])
                    except IntegrityError:
                        duplicate = await session.scalar(
                            select(MemoryEntry)
                            .where(
                                MemoryEntry.user_id == user_id,
                                MemoryEntry.content_hash == digest,
                            )
                            .with_for_update()
                        )
                        if duplicate is None:
                            raise
                        duplicate.last_confirmed_at = now
                        changed.append(
                            {
                                "id": duplicate.memory_id,
                                "event": "NONE",
                                "summary": duplicate.memory,
                                "entry": memory_snapshot(duplicate),
                            }
                        )
                        continue
                    old_value = None
                    outbox_type = "memory.upserted"
                else:
                    item = existing[str(action["memory_id"])]
                    old_value = item.memory
                    if event == "UPDATE":
                        value = normalize_memory(action["memory"])
                        if not value:
                            raise ValueError("UPDATE requires non-empty memory")
                        if value == old_value:
                            item.last_confirmed_at = now
                            changed.append(
                                {
                                    "id": item.memory_id,
                                    "event": "NONE",
                                    "summary": item.memory,
                                    "entry": memory_snapshot(item),
                                }
                            )
                            continue
                        duplicate = await session.scalar(
                            select(MemoryEntry.memory_id).where(
                                MemoryEntry.user_id == user_id,
                                MemoryEntry.content_hash == memory_hash(value),
                                MemoryEntry.memory_id != item.memory_id,
                            )
                        )
                        if duplicate is not None:
                            raise ApiError(
                                409,
                                "MEMORY_DUPLICATE",
                                "The new text duplicates another current memory",
                            )
                        item.memory = value
                        item.content_hash = memory_hash(value)
                        item.keywords = merge_keywords(value, action.get("keywords", []))
                        item.source = source
                        item.source_thread_id = source_thread_id
                        item.source_run_id = source_run_id
                        item.version += 1
                        item.updated_at = now
                        item.last_confirmed_at = now
                        outbox_type = "memory.upserted"
                    elif event == "DELETE":
                        item.source = source
                        item.source_thread_id = source_thread_id
                        item.source_run_id = source_run_id
                        item.version += 1
                        value = None
                        self._record(session, item, event, old_value, value, now)
                        snapshot = memory_snapshot(item)
                        await session.delete(item)
                        changed.append(
                            {
                                "id": item.memory_id,
                                "event": event,
                                "summary": old_value,
                                "entry": snapshot,
                            }
                        )
                        continue
                    else:
                        raise ValueError(f"unsupported memory event: {event}")
                self._record(session, item, event, old_value, value, now)
                session.add(self._outbox(item, outbox_type, now))
                changed.append(
                    {
                        "id": item.memory_id,
                        "event": event,
                        "summary": value or old_value or "",
                        "entry": memory_snapshot(item),
                    }
                )
        return changed

    @staticmethod
    def _record(
        session: Any,
        item: MemoryEntry,
        event: str,
        old_memory: str | None,
        new_memory: str | None,
        now: Any,
    ) -> None:
        session.add(
            MemoryHistory(
                history_id=uuid4().hex,
                memory_id=item.memory_id,
                user_id=item.user_id,
                event=event,
                memory_version=item.version,
                old_memory=old_memory,
                new_memory=new_memory,
                source_thread_id=item.source_thread_id,
                source_run_id=item.source_run_id,
                created_at=now,
            )
        )

    @staticmethod
    def _outbox(item: MemoryEntry, event_type: str, now: Any) -> OutboxEvent:
        return OutboxEvent(
            event_id=uuid4().hex,
            aggregate_type="memory",
            aggregate_id=item.memory_id,
            event_type=event_type,
            aggregate_version=item.version,
            payload_json=memory_snapshot(item),
            created_at=now,
            attempts=0,
        )


__all__ = [
    "MemoryAction",
    "MemoryService",
    "memory_hash",
    "memory_snapshot",
    "merge_keywords",
    "normalize_memory",
]
