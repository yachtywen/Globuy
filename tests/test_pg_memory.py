"""Plain-text memory contracts without paid model calls."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.api.errors import ApiError
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
from app.memory.keywords import extract_keywords
from app.memory.outbox_worker import MemoryOutboxWorker
from app.memory.postgres_store import PostgresMemoryStore, memory_decay_factor
from app.memory.service import MemoryService
from app.search.encoder import EmbeddingMetadata


class FakeEncoder:
    metadata = EmbeddingMetadata(model_id="fake-memory", revision="v2", dimensions=512)

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def encode_query(self, text: str) -> list[float]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * 512
        vector[0 if "黑色" in text else 1] = 1.0
        return vector


class FailingEncoder(FakeEncoder):
    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise RuntimeError("fake projection failure")


@pytest_asyncio.fixture
async def memory_database(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/memory.sqlite3")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = utc_naive()
    async with database.sessions.begin() as session:
        for user_id in ("user-1", "user-2"):
            session.add(User(
                user_id=user_id,
                email_normalized=f"{user_id}@example.com",
                password_hash="not-used",
                display_name=user_id,
                status="active",
                version=1,
                created_at=now,
                updated_at=now,
            ))
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_exact_duplicate_is_idempotent(memory_database) -> None:
    service = MemoryService(memory_database)
    first = await service.create("user-1", memory="我偏好轻便的通勤背包")
    duplicate = await service.create("user-1", memory="  我偏好轻便的通勤背包  ")
    assert duplicate["memory_id"] == first["memory_id"]
    assert len(await service.list("user-1")) == 1


@pytest.mark.asyncio
async def test_concurrent_exact_adds_leave_one_current_memory(memory_database) -> None:
    service = MemoryService(memory_database)

    async def add_once() -> list[dict]:
        return await service.apply_actions(
            "user-1",
            [{"event": "ADD", "memory": "我长期偏好静音键盘", "memory_id": None}],
            source_thread_id="thread-1",
            source_run_id="run-1",
        )

    first, second = await asyncio.gather(add_once(), add_once())
    assert first[0]["id"] == second[0]["id"]
    assert {first[0]["event"], second[0]["event"]} == {"ADD", "NONE"}
    assert len(await service.list("user-1")) == 1


@pytest.mark.asyncio
async def test_update_and_hard_delete_keep_read_only_history(memory_database) -> None:
    service = MemoryService(memory_database)
    created = await service.create("user-1", memory="我偏好黑色耳机")
    updated = await service.update(
        "user-1", created["memory_id"], memory="我现在偏好白色耳机"
    )
    assert updated["memory_id"] == created["memory_id"]
    assert updated["version"] == 2

    await service.delete("user-1", created["memory_id"])
    assert await service.list("user-1") == []
    history = await service.history("user-1", created["memory_id"])
    assert [item["event"] for item in history] == ["ADD", "UPDATE", "DELETE"]
    assert history[-1]["old_memory"] == "我现在偏好白色耳机"
    async with memory_database.sessions() as session:
        assert await session.get(MemoryEmbedding, created["memory_id"]) is None


@pytest.mark.asyncio
async def test_repeated_none_only_refreshes_confirmation(memory_database) -> None:
    service = MemoryService(memory_database)
    created = await service.create("user-1", memory="initial preference")
    before = created["last_confirmed_at"]
    changes = await service.apply_actions(
        "user-1",
        [{"event": "NONE", "memory": "", "memory_id": created["memory_id"]}],
        source_thread_id="thread-1",
        source_run_id="run-1",
    )
    assert changes[0]["event"] == "NONE"
    current = (await service.list("user-1"))[0]
    assert current["last_confirmed_at"] >= before
    assert current["version"] == 1
    assert [item["event"] for item in await service.history("user-1", created["memory_id"])] == [
        "ADD"
    ]


@pytest.mark.asyncio
async def test_action_batch_rejects_cross_user_id_without_writes(memory_database) -> None:
    service = MemoryService(memory_database)
    foreign = await service.create("user-2", memory="另一个用户的记忆")
    with pytest.raises(ValueError, match="unknown or cross-user"):
        await service.apply_actions(
            "user-1",
            [
                {"event": "ADD", "memory": "本批次不应落库", "memory_id": None},
                {"event": "UPDATE", "memory": "伪造更新", "memory_id": foreign["memory_id"]},
            ],
            source_thread_id="thread-1",
            source_run_id="run-1",
        )
    assert await service.list("user-1") == []


@pytest.mark.asyncio
async def test_action_batch_rejects_duplicate_targets(memory_database) -> None:
    service = MemoryService(memory_database)
    memory = await service.create("user-1", memory="初始文本")
    with pytest.raises(ValueError, match="at most once"):
        await service.apply_actions(
            "user-1",
            [
                {"event": "UPDATE", "memory": "新文本", "memory_id": memory["memory_id"]},
                {"event": "DELETE", "memory": "", "memory_id": memory["memory_id"]},
            ],
            source_thread_id=None,
            source_run_id=None,
        )
    current = await service.list("user-1")
    assert current[0]["memory"] == "初始文本"


@pytest.mark.asyncio
async def test_outbox_projection_and_deleted_memory_recall(memory_database) -> None:
    service = MemoryService(memory_database)
    memory = await service.create("user-1", memory="我偏好黑色耳机")
    settings = Settings(database_url=None, model_provider="mock")
    worker = MemoryOutboxWorker(memory_database, settings=settings, encoder=FakeEncoder())
    published = await worker.run_once()
    assert published["published"] == 1
    async with memory_database.sessions() as session:
        projection = await session.get(MemoryEmbedding, memory["memory_id"])
    assert projection is not None
    assert projection.semantic_text_version == "memory-text-v2"

    store = PostgresMemoryStore(memory_database, service, FakeEncoder(), settings)
    found = await store.asearch(
        ("users", "user-1", "memories"), query="黑色耳机", limit=5
    )
    assert [item.key for item in found] == [memory["memory_id"]]
    await service.delete("user-1", memory["memory_id"])
    assert await store.asearch(
        ("users", "user-1", "memories"), query="黑色耳机", limit=5
    ) == []


@pytest.mark.asyncio
async def test_decay_only_reorders_agent_recall_not_conflict_candidates(
    memory_database, monkeypatch
) -> None:
    service = MemoryService(memory_database)
    old = await service.create("user-1", memory="黑色耳机旧确认")
    fresh = await service.create("user-1", memory="黑色耳机新确认")
    async with memory_database.sessions.begin() as session:
        rows = list(
            (
                await session.scalars(
                    select(MemoryEntry).where(MemoryEntry.user_id == "user-1")
                )
            ).all()
        )
        by_id = {item.memory_id: item for item in rows}
        by_id[old["memory_id"]].last_confirmed_at = utc_naive() - timedelta(days=180)
        by_id[fresh["memory_id"]].last_confirmed_at = utc_naive()
    async with memory_database.sessions() as session:
        old_entry = await session.get(MemoryEntry, old["memory_id"])
        fresh_entry = await session.get(MemoryEntry, fresh["memory_id"])

    settings = Settings(database_url=None, model_provider="mock")
    store = PostgresMemoryStore(memory_database, service, FakeEncoder(), settings)

    async def vector_lane(*_args, **_kwargs):
        return [(old_entry, 1.0), (fresh_entry, 0.99)], 0

    async def keyword_lane(*_args, **_kwargs):
        return []

    monkeypatch.setattr(store, "_vector_lane", vector_lane)
    monkeypatch.setattr(store, "_keyword_lane", keyword_lane)
    recalled = await store.asearch(
        ("users", "user-1", "memories"), query="黑色耳机", limit=2
    )
    assert [item.key for item in recalled] == [fresh["memory_id"], old["memory_id"]]
    conflicts = await store.asearch_for_consolidation(
        "user-1", query="黑色耳机", limit=2
    )
    assert [item.key for item in conflicts] == [old["memory_id"], fresh["memory_id"]]


@pytest.mark.asyncio
async def test_history_is_user_isolated(memory_database) -> None:
    service = MemoryService(memory_database)
    memory = await service.create("user-1", memory="仅属于用户一")
    with pytest.raises(ApiError) as error:
        await service.history("user-2", memory["memory_id"])
    assert getattr(error.value, "code", None) == "MEMORY_NOT_FOUND"
    async with memory_database.sessions() as session:
        rows = list((await session.scalars(select(MemoryHistory))).all())
    assert {item.user_id for item in rows} == {"user-1"}


@pytest.mark.asyncio
async def test_outbox_uses_all_eight_backoffs_then_dead_letters(memory_database) -> None:
    service = MemoryService(memory_database)
    await service.create("user-1", memory="需要异步投影的记忆")
    settings = Settings(
        database_url=None,
        model_provider="mock",
        memory_outbox_max_attempts=8,
    )
    worker = MemoryOutboxWorker(memory_database, settings=settings, encoder=FailingEncoder())
    delays = (5, 10, 20, 40, 80, 160, 320, 600)
    for attempt, delay in enumerate(delays, start=1):
        assert (await worker.run_once())["failed"] == 1
        async with memory_database.sessions.begin() as session:
            event = await session.scalar(select(OutboxEvent))
            assert event.attempts == attempt
            assert event.dead_lettered_at is None
            assert event.available_at >= utc_naive() + timedelta(seconds=delay - 1)
            event.available_at = utc_naive()
    assert (await worker.run_once())["failed"] == 1
    async with memory_database.sessions() as session:
        event = await session.scalar(select(OutboxEvent))
        assert event.dead_lettered_at is not None
        assert event.available_at is None


def test_keyword_extraction_is_local_and_deterministic() -> None:
    first = extract_keywords("优先考虑 Sony WH-1000XM6 黑色头戴式耳机")
    second = extract_keywords("优先考虑 Sony WH-1000XM6 黑色头戴式耳机")
    assert first == second
    assert "sony" in first
    assert "wh-1000xm6" in first


@pytest.mark.parametrize(
    ("age_days", "expected"), [(0, 1.0), (90, 0.8), (180, 0.6), (365, 0.6)]
)
def test_memory_decay_factor_is_linear_with_a_floor(age_days: int, expected: float) -> None:
    assert memory_decay_factor(age_days) == pytest.approx(expected)
