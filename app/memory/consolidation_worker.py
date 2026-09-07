"""Persist short-term thread windows into long-term memories."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, or_, select

from app.agent.main_agent import main_agent
from app.auth.service import utc_naive
from app.config import Settings, get_settings
from app.database.models import MemoryConsolidationState, MemoryHistory, Message, Run
from app.database.session import Database
from app.memory.manager import MemoryManager
from app.memory.postgres_store import PostgresMemoryStore
from app.memory.service import MemoryService
from app.search.encoder import get_embedding_encoder

logger = logging.getLogger(__name__)
_RETRY_SECONDS = (60, 300, 1800)


class MemoryConsolidationWorker:
    def __init__(
        self,
        database: Database,
        manager: MemoryManager,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.database = database
        self.manager = manager
        self.settings = settings or get_settings()
        self._last_cleanup_at = None

    async def run_once(self) -> dict[str, int]:
        now = utc_naive()
        if self._last_cleanup_at is None or now - self._last_cleanup_at >= timedelta(days=1):
            try:
                await self.cleanup_history()
            except Exception:  # noqa: BLE001 - cleanup must not block consolidation
                logger.exception("Memory history cleanup failed")
            self._last_cleanup_at = now
        claim = await self._claim()
        if claim is None:
            return {"claimed": 0, "completed": 0, "failed": 0}
        token, thread_id, user_id, cursor = claim
        try:
            context, messages, run_id, snapshot_ordinal, run_count = await self._window(
                thread_id, cursor
            )
            if not messages:
                await self._complete(token, thread_id, cursor, 0)
                return {"claimed": 1, "completed": 1, "failed": 0}
            await self.manager.process(
                user_id=user_id,
                thread_id=thread_id,
                run_id=run_id,
                messages=messages,
                context_messages=context,
            )
            await self._complete(token, thread_id, snapshot_ordinal, run_count)
            return {"claimed": 1, "completed": 1, "failed": 0}
        except Exception as exc:  # noqa: BLE001 - durable state owns delayed retries
            logger.exception("Memory consolidation failed for thread_id=%s", thread_id)
            await self._fail(token, thread_id, type(exc).__name__)
            return {"claimed": 1, "completed": 0, "failed": 1}

    async def _claim(self) -> tuple[str, str, str, int] | None:
        now = utc_naive()
        expired = now - timedelta(seconds=self.settings.memory_consolidation_lease_seconds)
        token = uuid4().hex
        async with self.database.sessions.begin() as session:
            statement = (
                select(MemoryConsolidationState)
                .where(
                    MemoryConsolidationState.pending_successful_runs > 0,
                    MemoryConsolidationState.due_at <= now,
                    MemoryConsolidationState.dead_lettered_at.is_(None),
                    or_(
                        MemoryConsolidationState.claimed_at.is_(None),
                        MemoryConsolidationState.claimed_at < expired,
                    ),
                )
                .order_by(MemoryConsolidationState.due_at, MemoryConsolidationState.thread_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            state = await session.scalar(statement)
            if state is None:
                return None
            state.claimed_at = now
            state.claim_token = token
            return token, state.thread_id, state.user_id, state.processed_through_ordinal

    async def _window(
        self, thread_id: str, cursor: int
    ) -> tuple[list[dict[str, str]], list[dict[str, str]], str, int, int]:
        async with self.database.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Message)
                        .join(Run, Run.run_id == Message.run_id)
                        .where(
                            Message.thread_id == thread_id,
                            Message.ordinal > cursor,
                            Run.status == "succeeded",
                        )
                        .order_by(Message.ordinal)
                    )
                ).all()
            )
            selected_run_ids: list[str] = []
            for item in rows:
                if item.run_id not in selected_run_ids:
                    if len(selected_run_ids) >= self.settings.memory_consolidation_run_threshold:
                        break
                    selected_run_ids.append(item.run_id)
            selected = [item for item in rows if item.run_id in selected_run_ids]
            if not selected:
                return [], [], "", cursor, 0
            first_ordinal = selected[0].ordinal
            context_rows = list(
                (
                    await session.scalars(
                        select(Message)
                        .where(Message.thread_id == thread_id, Message.ordinal < first_ordinal)
                        .order_by(Message.ordinal.desc())
                        .limit(4)
                    )
                ).all()
            )
        context = [
            {"role": item.role, "content": item.content}
            for item in reversed(context_rows)
        ]
        messages = [{"role": item.role, "content": item.content} for item in selected]
        return context, messages, selected_run_ids[-1], selected[-1].ordinal, len(selected_run_ids)

    async def _complete(
        self, token: str, thread_id: str, snapshot_ordinal: int, run_count: int
    ) -> None:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            state = await session.get(
                MemoryConsolidationState, thread_id, with_for_update=True
            )
            if state is None or state.claim_token != token:
                return
            state.processed_through_ordinal = max(
                state.processed_through_ordinal, snapshot_ordinal
            )
            state.pending_successful_runs = (
                0
                if run_count == 0
                else max(0, state.pending_successful_runs - run_count)
            )
            state.claimed_at = None
            state.claim_token = None
            state.attempts = 0
            state.last_error_code = None
            state.updated_at = now
            state.due_at = now if state.pending_successful_runs else None

    async def _fail(self, token: str, thread_id: str, error_code: str) -> None:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            state = await session.get(
                MemoryConsolidationState, thread_id, with_for_update=True
            )
            if state is None or state.claim_token != token:
                return
            state.attempts += 1
            state.last_error_code = error_code[:100]
            state.claimed_at = None
            state.claim_token = None
            state.updated_at = now
            if state.attempts > len(_RETRY_SECONDS):
                state.dead_lettered_at = now
                state.due_at = None
            else:
                state.due_at = now + timedelta(seconds=_RETRY_SECONDS[state.attempts - 1])

    async def cleanup_history(self) -> int:
        cutoff = utc_naive() - timedelta(days=self.settings.memory_history_retention_days)
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                delete(MemoryHistory).where(MemoryHistory.created_at < cutoff)
            )
        return int(result.rowcount or 0)

    async def requeue(self, thread_id: str) -> bool:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            state = await session.get(
                MemoryConsolidationState, thread_id, with_for_update=True
            )
            if state is None or not state.pending_successful_runs:
                return False
            state.attempts = 0
            state.last_error_code = None
            state.dead_lettered_at = None
            state.claimed_at = None
            state.claim_token = None
            state.due_at = now
            state.updated_at = now
            return True


async def _main(serve: bool, requeue_thread: str | None) -> None:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("GLOBUY_DATABASE_URL is required")
    database = Database(
        settings.database_url.get_secret_value(),
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        pool_recycle=settings.database_pool_recycle_seconds,
    )
    service = MemoryService(database, settings)
    store = PostgresMemoryStore(database, service, get_embedding_encoder(), settings)
    manager = MemoryManager(
        model=main_agent.model,
        store=store,
        service=service,
        timeout_seconds=settings.memory_processing_timeout_seconds,
    )
    worker = MemoryConsolidationWorker(database, manager, settings=settings)
    try:
        if requeue_thread:
            print({"requeued": await worker.requeue(requeue_thread)})
        elif serve:
            while True:
                await worker.run_once()
                await asyncio.sleep(settings.memory_consolidation_poll_seconds)
        else:
            print(await worker.run_once())
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--requeue-thread")
    args = parser.parse_args()
    asyncio.run(_main(args.serve, args.requeue_thread))


if __name__ == "__main__":
    main()
