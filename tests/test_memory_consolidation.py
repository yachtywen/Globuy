"""Delayed thread-to-memory consolidation with deterministic fakes."""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.auth.service import utc_naive
from app.config import Settings
from app.database.models import (
    Base,
    MemoryConsolidationState,
    MemoryHistory,
    Message,
    Run,
    Thread,
    User,
)
from app.database.session import Database
from app.database.session_store import SQLAlchemySessionStore
from app.memory.consolidation_worker import MemoryConsolidationWorker


class CapturingManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def process(self, **kwargs):
        self.calls.append(kwargs)
        return []


@pytest_asyncio.fixture
async def consolidation_database(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/consolidation.sqlite3")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = utc_naive()
    async with database.sessions.begin() as session:
        session.add(
            User(
                user_id="user-1",
                email_normalized="user-1@example.com",
                password_hash="unused",
                display_name="User",
                status="active",
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            Thread(
                thread_id="thread-1",
                user_id="user-1",
                title="Thread",
                status="active",
                active_slot=1,
                created_at=now,
                updated_at=now,
            )
        )
    yield database
    await database.close()


async def _finish_run(
    database: Database,
    store: SQLAlchemySessionStore,
    index: int,
    status: str = "succeeded",
) -> None:
    now = utc_naive()
    run_id = f"run-{index}"
    async with database.sessions.begin() as session:
        session.add(
            Run(
                run_id=run_id,
                thread_id="thread-1",
                status="running",
                query=f"query {index}",
                attempt=1,
                created_at=now,
                started_at=now,
            )
        )
        session.add(
            Message(
                message_id=f"user-message-{index}",
                thread_id="thread-1",
                run_id=run_id,
                role="user",
                content=f"user fact {index}",
                is_partial=False,
                ordinal=index * 2 - 1,
                created_at=now,
            )
        )
    await store.finish_run(
        thread_id="thread-1",
        run_id=run_id,
        status=status,
        final_text=f"answer {index}",
        result={},
        message_id=f"assistant-message-{index}",
        is_partial=False,
    )


@pytest.mark.asyncio
async def test_tenth_successful_run_becomes_due_and_failure_does_not_count(
    consolidation_database,
) -> None:
    store = SQLAlchemySessionStore(
        consolidation_database, memory_run_threshold=10, memory_idle_seconds=900
    )
    for index in range(1, 10):
        await _finish_run(consolidation_database, store, index)
    async with consolidation_database.sessions() as session:
        state = await session.get(MemoryConsolidationState, "thread-1")
        assert state.pending_successful_runs == 9
        assert state.due_at > utc_naive() + timedelta(minutes=14)

    await _finish_run(consolidation_database, store, 10, status="failed")
    async with consolidation_database.sessions() as session:
        state = await session.get(MemoryConsolidationState, "thread-1")
        assert state.pending_successful_runs == 9
        assert state.due_at > utc_naive() + timedelta(minutes=14)

    await _finish_run(consolidation_database, store, 11)
    async with consolidation_database.sessions() as session:
        state = await session.get(MemoryConsolidationState, "thread-1")
        assert state.pending_successful_runs == 10
        assert state.due_at <= utc_naive()


@pytest.mark.asyncio
async def test_worker_processes_at_most_ten_runs_and_advances_snapshot_cursor(
    consolidation_database,
) -> None:
    now = utc_naive()
    async with consolidation_database.sessions.begin() as session:
        for index in range(1, 12):
            run_id = f"run-{index}"
            session.add(
                Run(
                    run_id=run_id,
                    thread_id="thread-1",
                    status="succeeded",
                    query=f"query {index}",
                    attempt=1,
                    created_at=now,
                    finished_at=now,
                )
            )
            session.add_all(
                [
                    Message(
                        message_id=f"u-{index}",
                        thread_id="thread-1",
                        run_id=run_id,
                        role="user",
                        content=f"fact {index}",
                        is_partial=False,
                        ordinal=index * 2 - 1,
                        created_at=now,
                    ),
                    Message(
                        message_id=f"a-{index}",
                        thread_id="thread-1",
                        run_id=run_id,
                        role="assistant",
                        content=f"answer {index}",
                        is_partial=False,
                        ordinal=index * 2,
                        created_at=now,
                    ),
                ]
            )
        session.add(
            MemoryConsolidationState(
                thread_id="thread-1",
                user_id="user-1",
                processed_through_ordinal=0,
                pending_successful_runs=11,
                due_at=now - timedelta(seconds=1),
                attempts=0,
                updated_at=now,
            )
        )

    manager = CapturingManager()
    settings = Settings(
        database_url=None,
        model_provider="mock",
        memory_consolidation_run_threshold=10,
    )
    worker = MemoryConsolidationWorker(consolidation_database, manager, settings=settings)
    assert await worker.run_once() == {"claimed": 1, "completed": 1, "failed": 0}
    assert len(manager.calls[0]["messages"]) == 20
    assert manager.calls[0]["context_messages"] == []
    async with consolidation_database.sessions() as session:
        state = await session.get(MemoryConsolidationState, "thread-1")
        assert state.processed_through_ordinal == 20
        assert state.pending_successful_runs == 1
        assert state.due_at <= utc_naive()


@pytest.mark.asyncio
async def test_worker_uses_all_three_backoffs_then_dead_letters(consolidation_database) -> None:
    now = utc_naive()
    async with consolidation_database.sessions.begin() as session:
        session.add(
            MemoryConsolidationState(
                thread_id="thread-1",
                user_id="user-1",
                processed_through_ordinal=0,
                pending_successful_runs=1,
                due_at=now,
                claimed_at=now,
                claim_token="claim",
                attempts=0,
                updated_at=now,
            )
        )
    worker = MemoryConsolidationWorker(
        consolidation_database,
        CapturingManager(),
        settings=Settings(database_url=None, model_provider="mock"),
    )
    expected = (60, 300, 1800)
    for attempts, delay in enumerate(expected, start=1):
        await worker._fail("claim", "thread-1", "FakeError")
        async with consolidation_database.sessions.begin() as session:
            state = await session.get(MemoryConsolidationState, "thread-1")
            assert state.attempts == attempts
            assert state.dead_lettered_at is None
            assert state.due_at >= utc_naive() + timedelta(seconds=delay - 1)
            state.claim_token = "claim"
            state.claimed_at = utc_naive()
    await worker._fail("claim", "thread-1", "FakeError")
    async with consolidation_database.sessions() as session:
        state = await session.get(MemoryConsolidationState, "thread-1")
        assert state.dead_lettered_at is not None
        assert state.due_at is None


@pytest.mark.asyncio
async def test_archiving_thread_makes_pending_window_immediately_due(
    consolidation_database,
) -> None:
    store = SQLAlchemySessionStore(
        consolidation_database, memory_run_threshold=10, memory_idle_seconds=900
    )
    await _finish_run(consolidation_database, store, 1)
    await store.replace_thread(
        user_id="user-1",
        current_thread_id="thread-1",
        client_request_id="replace-1",
        new_thread_id="thread-2",
    )
    async with consolidation_database.sessions() as session:
        state = await session.get(MemoryConsolidationState, "thread-1")
        assert state.due_at <= utc_naive()
        archived = await session.get(Thread, "thread-1")
        assert archived.status == "archived"


@pytest.mark.asyncio
async def test_history_cleanup_keeps_records_inside_180_day_boundary(
    consolidation_database,
) -> None:
    now = utc_naive()
    async with consolidation_database.sessions.begin() as session:
        session.add_all(
            [
                MemoryHistory(
                    history_id="expired",
                    memory_id="old-memory",
                    user_id="user-1",
                    event="DELETE",
                    memory_version=2,
                    old_memory="old",
                    new_memory=None,
                    source_thread_id="thread-1",
                    source_run_id="run-old",
                    created_at=now - timedelta(days=181),
                ),
                MemoryHistory(
                    history_id="retained",
                    memory_id="recent-memory",
                    user_id="user-1",
                    event="ADD",
                    memory_version=1,
                    old_memory=None,
                    new_memory="recent",
                    source_thread_id="thread-1",
                    source_run_id="run-recent",
                    created_at=now - timedelta(days=179),
                ),
            ]
        )
    worker = MemoryConsolidationWorker(
        consolidation_database,
        CapturingManager(),
        settings=Settings(database_url=None, model_provider="mock"),
    )
    assert await worker.cleanup_history() == 1
    async with consolidation_database.sessions() as session:
        rows = list((await session.scalars(select(MemoryHistory.history_id))).all())
        assert rows == ["retained"]
