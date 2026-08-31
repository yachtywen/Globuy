"""Executable, deterministic long-term-memory integration evaluation."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, select, text

from app.api.errors import ApiError
from app.auth.service import utc_naive
from app.config import Settings
from app.database.models import (
    Base,
    MemoryCandidate,
    MemoryEmbedding,
    MemoryEntry,
    MemoryVersion,
    OutboxEvent,
    User,
)
from app.database.services import MemoryService
from app.database.session import Database
from app.memory.outbox_worker import MemoryOutboxWorker
from app.memory.postgres_store import PostgresMemoryStore, current_memory_recall_metrics
from app.search.encoder import EmbeddingMetadata


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryAction(StrictModel):
    op: Literal["candidate", "confirm", "project", "recall", "delete", "restore", "list"]
    alias: str | None = None
    target: str | None = None
    user: str = "primary"
    category: Literal["blacklist", "preference", "history"] = "preference"
    key: str | None = None
    content: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    persistence_scope: Literal["long_term", "session_only"] = "long_term"
    subject: str | None = None
    predicate: str | None = None
    value_json: Any | None = None
    polarity: Literal["positive", "negative"] | None = None
    scope_type: Literal["global", "category", "brand", "product"] | None = None
    scope_value: str | None = None
    evidence_type: Literal["explicit", "inferred", "imported"] = "explicit"
    query: str | None = None


class MemoryAssertion(StrictModel):
    type: Literal[
        "error_code",
        "same_memory",
        "different_memory",
        "reinforcement_count",
        "supersedes",
        "recall_contains",
        "recall_excludes",
        "recall_before",
        "field_equals",
        "active_count",
        "archived_count",
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
    schema_version: Literal["1.0"] = "1.0"
    cases: list[MemoryEvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> MemoryCaseFile:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate memory case id")
        return self


class FakeMemoryEncoder:
    metadata = EmbeddingMetadata(model_id="memory-eval", revision="v1", dimensions=1024)

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.casefold()
        index = 0 if any(word in lowered for word in ("黑色", "深色", "black")) else 1
        result = [0.0] * 1024
        result[index] = 1.0
        return result

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def encode_query(self, text: str) -> list[float]:
        return self._vector(text)


async def _database(
    url: str | None,
) -> tuple[Database, tempfile.TemporaryDirectory[str] | None]:
    if url:
        database = Database(url)
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        return database, None
    temp_dir = tempfile.TemporaryDirectory(prefix="globuy-memory-eval-")
    temp = Path(temp_dir.name) / "memory.sqlite3"
    database = Database(f"sqlite+aiosqlite:///{temp.as_posix()}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return database, temp_dir


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
    prompt_path = Path(__file__).resolve().parents[1] / "prompt" / "prompts.yml"
    prompt_fingerprint = hashlib.sha256(prompt_path.read_bytes()).hexdigest()[:16]
    try:
        for case in case_file.cases:
            case_started = time.monotonic()
            user_ids = {
                name: f"memory-eval-{case.id}-{name}-{uuid4().hex[:8]}"
                for name in {action.user for action in case.actions}
            }
            now = utc_naive()
            async with database.sessions.begin() as session:
                for name, user_id in user_ids.items():
                    session.add(
                        User(
                            user_id=user_id,
                            email_normalized=f"{user_id}@example.invalid",
                            password_hash="eval-only",
                            display_name=name,
                            status="active",
                            version=1,
                            created_at=now,
                            updated_at=now,
                        )
                    )
            service = MemoryService(database, settings=settings)
            worker = MemoryOutboxWorker(database, settings=settings, encoder=encoder)
            store = PostgresMemoryStore(database, service, encoder, settings)
            state: dict[str, Any] = {
                "errors": {},
                "recalls": {},
                "recall_metrics": {},
                "lists": {},
            }
            try:
                for action in case.actions:
                    user_id = user_ids[action.user]
                    try:
                        if action.op == "candidate":
                            value = await service.create_candidate(
                                user_id,
                                category=action.category,
                                key=action.key or action.alias or "memory",
                                content=action.content or "",
                                confidence=Decimal(str(action.confidence)),
                                source_thread_id=None,
                                source_run_id=None,
                                persistence_scope=action.persistence_scope,
                                subject=action.subject,
                                predicate=action.predicate,
                                value_json=action.value_json,
                                polarity=action.polarity,
                                scope_type=action.scope_type,
                                scope_value=action.scope_value,
                                evidence_type=action.evidence_type,
                            )
                            state[action.alias or "candidate"] = value
                        elif action.op == "confirm":
                            candidate = state[action.target or "candidate"]
                            value = await service.confirm_candidate(
                                user_id, candidate["candidate_id"]
                            )
                            state[action.alias or action.target or "memory"] = value
                        elif action.op == "project":
                            state[action.alias or "project"] = await worker.run_once()
                        elif action.op == "recall":
                            found = await store.asearch(
                                ("users", user_id, "memories"),
                                query=action.query,
                                limit=10,
                            )
                            state["recalls"][action.alias or "recall"] = [
                                item.key for item in found
                            ]
                            state["recall_metrics"][action.alias or "recall"] = (
                                current_memory_recall_metrics()
                            )
                        elif action.op == "delete":
                            memory_id = state[action.target or "memory"]["memory_id"]
                            await service.delete(user_id, memory_id)
                        elif action.op == "restore":
                            target = action.target or "memory"
                            state[action.alias or target] = await service.restore(
                                user_id, state[target]["memory_id"]
                            )
                        elif action.op == "list":
                            state["lists"][action.alias or "active"] = await service.list(
                                user_id, lifecycle_status=action.content or "active"
                            )
                    except ApiError as exc:
                        state["errors"][action.alias or action.op] = exc.code
                failures = _assertions(case.assertions, state)
                async with database.sessions() as session:
                    memory_ids = list(
                        (
                            await session.scalars(
                                select(MemoryEntry.memory_id).where(
                                    MemoryEntry.user_id.in_(user_ids.values())
                                )
                            )
                        ).all()
                    )
                    evidence = {
                        "candidate_count": int(
                            await session.scalar(
                                select(func.count())
                                .select_from(MemoryCandidate)
                                .where(MemoryCandidate.user_id.in_(user_ids.values()))
                            )
                            or 0
                        ),
                        "memory_count": len(memory_ids),
                        "version_count": (
                            int(
                                await session.scalar(
                                    select(func.count())
                                    .select_from(MemoryVersion)
                                    .where(MemoryVersion.memory_id.in_(memory_ids))
                                )
                                or 0
                            )
                            if memory_ids
                            else 0
                        ),
                        "projection_count": (
                            int(
                                await session.scalar(
                                    select(func.count())
                                    .select_from(MemoryEmbedding)
                                    .where(MemoryEmbedding.memory_id.in_(memory_ids))
                                )
                                or 0
                            )
                            if memory_ids
                            else 0
                        ),
                        "recalls": state["recall_metrics"],
                    }
            finally:
                async with database.sessions.begin() as session:
                    memory_ids = list(
                        (
                            await session.scalars(
                                select(MemoryEntry.memory_id).where(
                                    MemoryEntry.user_id.in_(user_ids.values())
                                )
                            )
                        ).all()
                    )
                    if memory_ids:
                        await session.execute(
                            delete(OutboxEvent).where(
                                OutboxEvent.aggregate_type == "memory",
                                OutboxEvent.aggregate_id.in_(memory_ids),
                            )
                        )
                    for user_id in user_ids.values():
                        await session.execute(delete(User).where(User.user_id == user_id))
            results.append(
                {
                    "case_id": case.id,
                    "description": case.description,
                    "verdict": "PASS" if not failures else "FAIL",
                    "p0_pass": not failures,
                    "failures": failures,
                    "duration_ms": round((time.monotonic() - case_started) * 1000),
                    "evidence": evidence,
                }
            )
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
        "# Long-term Memory Evaluation",
        "",
        f"- Generated: {datetime.now(UTC).isoformat()}",
        f"- Backend: {'sqlite-test-double' if temp_dir else 'postgresql-pgvector'}",
        f"- Migration: `{migration_version}`",
        f"- Prompt fingerprint: `{prompt_fingerprint}`",
        "- Embedding: `memory-eval@v1:1024:normalized`",
        "- Extraction: `memory-fact-v2`",
        f"- Result: {passed}/{len(results)} PASS",
        "",
    ]
    report.extend(
        f"- **{item['verdict']}** `{item['case_id']}`"
        + (f": {'; '.join(item['failures'])}" if item["failures"] else "")
        + (
            f" ({item['duration_ms']} ms; candidates={item['evidence']['candidate_count']}; "
            f"memories={item['evidence']['memory_count']}; "
            f"versions={item['evidence']['version_count']}; "
            f"projections={item['evidence']['projection_count']})"
        )
        for item in results
    )
    (output_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return passed == len(results)


def _assertions(assertions: list[MemoryAssertion], state: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for assertion in assertions:
        ok = False
        if assertion.type == "error_code":
            ok = state["errors"].get(assertion.left) == assertion.value
        elif assertion.type == "same_memory":
            ok = state[assertion.left]["memory_id"] == state[assertion.right]["memory_id"]
        elif assertion.type == "different_memory":
            ok = state[assertion.left]["memory_id"] != state[assertion.right]["memory_id"]
        elif assertion.type == "reinforcement_count":
            ok = state[assertion.left]["reinforcement_count"] == assertion.value
        elif assertion.type == "supersedes":
            ok = (
                state[assertion.left]["supersedes_memory_id"] == state[assertion.right]["memory_id"]
            )
        elif assertion.type == "recall_contains":
            ok = assertion.value in state["recalls"].get(assertion.left, [])
        elif assertion.type == "recall_excludes":
            ok = assertion.value not in state["recalls"].get(assertion.left, [])
        elif assertion.type == "recall_before":
            recalled = state["recalls"].get(assertion.left, [])
            ok = (
                assertion.value in recalled
                and assertion.right in recalled
                and recalled.index(assertion.value) < recalled.index(assertion.right)
            )
        elif assertion.type == "field_equals":
            ok = state[assertion.left].get(assertion.right) == assertion.value
        elif assertion.type in {"active_count", "archived_count"}:
            ok = len(state["lists"].get(assertion.left, [])) == assertion.value
        if not ok:
            failures.append(f"{assertion.type}:{assertion.left}:{assertion.value}")
    return failures


__all__ = ["MemoryCaseFile", "run_memory_suite"]
