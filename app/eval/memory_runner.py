"""Deterministic evaluation runner for plain-text long-term memory."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, select, text

from app.auth.service import utc_naive
from app.config import Settings
from app.database.models import (
    Base,
    MemoryEmbedding,
    MemoryEntry,
    MemoryHistory,
    OutboxEvent,
    User,
)
from app.database.session import Database
from app.memory.outbox_worker import MemoryOutboxWorker
from app.memory.postgres_store import PostgresMemoryStore, current_memory_recall_metrics
from app.memory.service import MemoryService
from app.search.encoder import EmbeddingMetadata


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryAction(StrictModel):
    op: Literal["add", "update", "delete", "confirm", "project", "recall", "list", "history"]
    alias: str | None = None
    target: str | None = None
    user: str = "primary"
    memory: str | None = None
    query: str | None = None


class MemoryAssertion(StrictModel):
    type: Literal[
        "same_memory",
        "field_equals",
        "recall_contains",
        "recall_excludes",
        "active_count",
        "history_events",
    ]
    left: str | None = None
    right: str | None = None
    value: Any = None


class MemoryEvaluationCase(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    description: str
    actions: list[MemoryAction] = Field(min_length=1)
    assertions: list[MemoryAssertion] = Field(min_length=1)


class MemoryCaseFile(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    cases: list[MemoryEvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> MemoryCaseFile:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate memory case id")
        return self


class FakeMemoryEncoder:
    metadata = EmbeddingMetadata(model_id="memory-eval", revision="v2", dimensions=512)

    @staticmethod
    def _vector(value: str) -> list[float]:
        digest = hashlib.sha256(value.casefold().encode()).digest()
        vector = [0.0] * 512
        for index in range(0, len(digest), 2):
            vector[int.from_bytes(digest[index:index + 2]) % 512] += 1.0
        norm = sum(item * item for item in vector) ** 0.5 or 1.0
        return [item / norm for item in vector]

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def encode_query(self, text: str) -> list[float]:
        return self._vector(text)


async def _database(url: str | None) -> tuple[Database, tempfile.TemporaryDirectory[str] | None]:
    if url:
        database = Database(url)
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        return database, None
    temp_dir = tempfile.TemporaryDirectory(prefix="globuy-memory-eval-")
    path = Path(temp_dir.name) / "memory.sqlite3"
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return database, temp_dir


def _assertions(assertions: list[MemoryAssertion], state: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for assertion in assertions:
        if assertion.type == "same_memory":
            ok = state[assertion.left]["memory_id"] == state[assertion.right]["memory_id"]
        elif assertion.type == "field_equals":
            ok = state[assertion.left].get(assertion.right) == assertion.value
        elif assertion.type == "recall_contains":
            ok = state[assertion.left]["memory_id"] in state["recalls"].get(
                str(assertion.value), []
            )
        elif assertion.type == "recall_excludes":
            ok = state[assertion.left]["memory_id"] not in state["recalls"].get(
                str(assertion.value), []
            )
        elif assertion.type == "active_count":
            ok = len(state["lists"].get(assertion.left, [])) == assertion.value
        elif assertion.type == "history_events":
            events = [item["event"] for item in state["histories"].get(assertion.left, [])]
            ok = events == assertion.value
        else:
            ok = False
        if not ok:
            actual = (
                [
                    item["event"]
                    for item in state["histories"].get(assertion.left, [])
                ]
                if assertion.type == "history_events"
                else None
            )
            failures.append(
                f"{assertion.type}:{assertion.left}:expected={assertion.value}:actual={actual}"
            )
    return failures


async def run_memory_suite(
    cases_path: Path, output_dir: Path, *, database_url: str | None = None
) -> bool:
    case_file = MemoryCaseFile.model_validate(yaml.safe_load(cases_path.read_text("utf-8")))
    database, temp_dir = await _database(database_url)
    settings = Settings(database_url=None, model_provider="mock")
    encoder = FakeMemoryEncoder()
    results: list[dict[str, Any]] = []
    async with database.sessions() as session:
        try:
            migration_version = str(
                await session.scalar(text("SELECT version_num FROM alembic_version"))
            )
        except Exception:
            migration_version = "metadata-current"
    try:
        for case in case_file.cases:
            started = time.monotonic()
            users = {
                name: f"memory-eval-{case.id}-{name}-{uuid4().hex[:8]}"
                for name in {action.user for action in case.actions}
            }
            now = utc_naive()
            async with database.sessions.begin() as session:
                for name, user_id in users.items():
                    session.add(User(
                        user_id=user_id,
                        email_normalized=f"{user_id}@example.invalid",
                        password_hash="eval-only",
                        display_name=name,
                        status="active",
                        version=1,
                        created_at=now,
                        updated_at=now,
                    ))
            service = MemoryService(database)
            worker = MemoryOutboxWorker(database, settings=settings, encoder=encoder)
            store = PostgresMemoryStore(database, service, encoder, settings)
            state: dict[str, Any] = {"recalls": {}, "lists": {}, "histories": {}}
            for action in case.actions:
                user_id = users[action.user]
                alias = action.alias or action.target or action.op
                if action.op == "add":
                    state[alias] = await service.create(user_id, memory=action.memory or "")
                elif action.op == "update":
                    target = state[action.target or "memory"]
                    state[alias] = await service.update(
                        user_id, target["memory_id"], memory=action.memory or ""
                    )
                elif action.op == "delete":
                    await service.delete(user_id, state[action.target or "memory"]["memory_id"])
                elif action.op == "confirm":
                    target = state[action.target or "memory"]
                    state[alias] = (
                        await service.apply_actions(
                            user_id,
                            [{"event": "NONE", "memory": "", "memory_id": target["memory_id"]}],
                            source_thread_id="memory-eval",
                            source_run_id=case.id,
                        )
                    )[0]["entry"]
                elif action.op == "project":
                    state[alias] = await worker.run_once()
                elif action.op == "recall":
                    found = await store.asearch(
                        ("users", user_id, "memories"), query=action.query, limit=10
                    )
                    state["recalls"][alias] = [item.key for item in found]
                    state[f"{alias}_metrics"] = current_memory_recall_metrics()
                elif action.op == "list":
                    state["lists"][alias] = await service.list(user_id)
                elif action.op == "history":
                    state["histories"][alias] = await service.history(
                        user_id, state[action.target or "memory"]["memory_id"]
                    )
            failures = _assertions(case.assertions, state)
            async with database.sessions() as session:
                memory_ids = list((await session.scalars(
                    select(MemoryEntry.memory_id).where(MemoryEntry.user_id.in_(users.values()))
                )).all())
                evidence = {
                    "memory_count": len(memory_ids),
                    "history_count": int(await session.scalar(
                        select(func.count()).select_from(MemoryHistory).where(
                            MemoryHistory.user_id.in_(users.values())
                        )
                    ) or 0),
                    "projection_count": int(await session.scalar(
                        select(func.count()).select_from(MemoryEmbedding).where(
                            MemoryEmbedding.memory_id.in_(memory_ids)
                        )
                    ) or 0) if memory_ids else 0,
                }
            async with database.sessions.begin() as session:
                if memory_ids:
                    await session.execute(delete(OutboxEvent).where(
                        OutboxEvent.aggregate_type == "memory",
                        OutboxEvent.aggregate_id.in_(memory_ids),
                    ))
                for user_id in users.values():
                    await session.execute(delete(User).where(User.user_id == user_id))
            results.append({
                "case_id": case.id,
                "description": case.description,
                "verdict": "PASS" if not failures else "FAIL",
                "failures": failures,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "evidence": evidence,
            })
    finally:
        await database.close()
        if temp_dir is not None:
            temp_dir.cleanup()

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "case-results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    passed = sum(item["verdict"] == "PASS" for item in results)
    report = [
        "# Plain-text Long-term Memory Evaluation",
        "",
        f"- Generated: {datetime.now(UTC).isoformat()}",
        f"- Migration: `{migration_version}`",
        "- Embedding: `memory-eval@v2:512:normalized`",
        f"- Result: {passed}/{len(results)} PASS",
        "",
        *[
            f"- **{item['verdict']}** `{item['case_id']}`: {'; '.join(item['failures']) or 'ok'}"
            for item in results
        ],
    ]
    (output_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return passed == len(results)


__all__ = ["MemoryCaseFile", "run_memory_suite"]
