"""PostgreSQL/pgvector memory contracts without paid model calls."""

from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.auth.service import utc_naive
from app.config import Settings
from app.database.models import Base, MemoryEmbedding, MemoryEntry, OutboxEvent, User
from app.database.services import MemoryService
from app.database.session import Database
from app.memory.keywords import extract_keywords
from app.memory.outbox_worker import MemoryOutboxWorker
from app.memory.postgres_store import PostgresMemoryStore
from app.search.encoder import EmbeddingMetadata


class FakeEncoder:
    metadata = EmbeddingMetadata(model_id="fake-memory", revision="v1", dimensions=1024)

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def encode_query(self, text: str) -> list[float]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        if "黑色" in text or "深色" in text:
            return [1.0, *([0.0] * 1023)]
        return [0.0, 1.0, *([0.0] * 1022)]


@pytest_asyncio.fixture
async def memory_database(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/memory.sqlite3")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = utc_naive()
    async with database.sessions.begin() as session:
        session.add(
            User(
                user_id="user-1",
                email_normalized="memory@example.com",
                password_hash="not-used",
                display_name="Memory User",
                status="active",
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_candidate_requires_confirmation_before_projection(memory_database) -> None:
    service = MemoryService(memory_database)
    candidate = await service.create_candidate(
        "user-1",
        category="preference",
        key="color",
        content="偏好黑色和深色商品",
        confidence=Decimal("0.9"),
        source_thread_id=None,
        source_run_id=None,
    )
    assert await service.list("user-1") == []

    memory = await service.confirm_candidate("user-1", candidate["candidate_id"])
    assert memory["source"] == "agent_confirmed"
    assert memory["keywords"]

    worker = MemoryOutboxWorker(
        memory_database,
        settings=Settings(database_url=None, model_provider="mock"),
        encoder=FakeEncoder(),
    )
    result = await worker.run_once()
    assert result["published"] == 1
    async with memory_database.sessions() as session:
        projection = await session.get(MemoryEmbedding, memory["memory_id"])
    assert projection is not None
    assert projection.embedding_model == "fake-memory"


@pytest.mark.asyncio
async def test_pgvector_store_returns_blacklist_before_decayed_preferences(
    memory_database,
) -> None:
    service = MemoryService(memory_database)
    preference = await service.create(
        "user-1",
        category="preference",
        key="color",
        content="偏好黑色商品",
        confidence=Decimal("1"),
        source_thread_id=None,
        source_run_id=None,
    )
    await service.create(
        "user-1",
        category="blacklist",
        key="material",
        content="不要塑料材质",
        confidence=Decimal("1"),
        source_thread_id=None,
        source_run_id=None,
    )
    worker = MemoryOutboxWorker(
        memory_database,
        settings=Settings(database_url=None, model_provider="mock"),
        encoder=FakeEncoder(),
    )
    await worker.run_once()
    old = utc_naive() - timedelta(days=180)
    async with memory_database.sessions.begin() as session:
        item = await session.get(MemoryEntry, preference["memory_id"])
        assert item is not None
        item.last_reinforced_at = old

    store = PostgresMemoryStore(
        memory_database,
        service,
        FakeEncoder(),
        Settings(database_url=None, model_provider="mock"),
    )
    found = await store.asearch(("users", "user-1", "memories"), query="想买深色耳机", limit=5)
    assert found[0].key == "material"
    assert any(item.key == "color" and item.score is not None for item in found)


def test_keyword_extraction_is_local_and_deterministic() -> None:
    first = extract_keywords("优先考虑 Sony WH-1000XM6 黑色头戴式耳机")
    second = extract_keywords("优先考虑 Sony WH-1000XM6 黑色头戴式耳机")
    assert first == second
    assert "sony" in first
    assert "wh-1000xm6" in first


@pytest.mark.asyncio
async def test_real_postgres_pgvector_projection_and_recall() -> None:
    database_url = os.getenv("GLOBUY_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("GLOBUY_TEST_POSTGRES_URL is not configured")
    database = Database(database_url)
    user_id = f"pg-memory-{uuid4().hex}"
    memory_id: str | None = None
    now = utc_naive()
    try:
        async with database.sessions.begin() as session:
            session.add(
                User(
                    user_id=user_id,
                    email_normalized=f"{user_id}@example.com",
                    password_hash="not-used",
                    display_name="PostgreSQL Memory User",
                    status="active",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        service = MemoryService(database)
        memory = await service.create(
            user_id,
            category="preference",
            key="postgres-color",
            content="\u504f\u597d\u9ed1\u8272\u548c\u6df1\u8272\u5546\u54c1",
            confidence=Decimal("0.9"),
            source_thread_id=None,
            source_run_id=None,
        )
        memory_id = memory["memory_id"]
        worker = MemoryOutboxWorker(
            database,
            settings=Settings(database_url=database_url, model_provider="mock"),
            encoder=FakeEncoder(),
        )
        result = await worker.run_once()
        assert result["published"] >= 1
        async with database.sessions() as session:
            projection = await session.get(MemoryEmbedding, memory["memory_id"])
        assert projection is not None
        assert len(projection.embedding) == 1024

        store = PostgresMemoryStore(
            database,
            service,
            FakeEncoder(),
            Settings(database_url=database_url, model_provider="mock"),
        )
        found = await store.asearch(
            ("users", user_id, "memories"),
            query="\u6211\u60f3\u4e70\u6df1\u8272\u8033\u673a",
            limit=5,
        )
        assert any(item.key == "postgres-color" for item in found)
    finally:
        async with database.sessions.begin() as session:
            if memory_id is not None:
                await session.execute(
                    delete(OutboxEvent).where(OutboxEvent.aggregate_id == memory_id)
                )
            await session.execute(delete(User).where(User.user_id == user_id))
        await database.close()
