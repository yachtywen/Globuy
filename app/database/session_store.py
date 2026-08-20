"""SQLAlchemy implementation of the existing session/run persistence contract."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.api.errors import ApiError
from app.api.storage import TERMINAL_RUN_STATUSES
from app.database.models import (
    Artifact,
    IdempotencyKey,
    Message,
    Run,
    RunResult,
    Thread,
    User,
)
from app.database.session import Database


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _title(query: str) -> str:
    return " ".join(query.split())[:32] or "新对话"


def _encode_cursor(timestamp: str, thread_id: str) -> str:
    raw = json.dumps([timestamp, thread_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        timestamp = datetime.fromisoformat(str(value[0]).replace("Z", "+00:00"))
        return timestamp.replace(tzinfo=None), str(value[1])
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApiError(422, "INVALID_CURSOR", "归档游标无效") from exc


def _thread_dict(thread: Thread) -> dict[str, Any]:
    return {
        "thread_id": thread.thread_id,
        "user_id": thread.user_id,
        "title": thread.title,
        "status": thread.status,
        "created_at": _iso(thread.created_at),
        "updated_at": _iso(thread.updated_at),
        "archived_at": _iso(thread.archived_at),
        "archive_reason": thread.archive_reason,
        "last_run_id": thread.last_run_id,
    }


def _artifact_dict(item: Artifact) -> dict[str, Any]:
    return {
        "file_id": item.file_id,
        "thread_id": item.thread_id,
        "run_id": item.run_id,
        "filename": item.filename,
        "kind": item.kind,
        "media_type": item.media_type,
        "size": item.size,
        "relative_path": item.relative_path,
        "sha256": item.sha256,
        "created_at": _iso(item.created_at),
    }


class SQLAlchemySessionStore:
    """PostgreSQL-backed store preserving the RunRegistry storage protocol."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def open(self) -> None:
        await self.database.ping()

    async def close(self) -> None:
        await self.database.close()

    async def recover_after_restart(self) -> dict[str, int]:
        now = _now()
        async with self.database.sessions.begin() as session:
            interrupted = await session.execute(
                update(Run)
                .where(Run.status.in_(("starting", "running", "cancelling")))
                .values(
                    status="interrupted",
                    finished_at=now,
                    error_code="SERVER_RESTART",
                    error_message="服务重启导致任务中断",
                )
            )
            occupied = select(Run.thread_id).union(select(Message.thread_id))
            archived = await session.execute(
                update(Thread)
                .where(Thread.status == "active", Thread.thread_id.in_(occupied))
                .values(
                    status="archived",
                    active_slot=None,
                    archived_at=now,
                    updated_at=now,
                    archive_reason="server_restart",
                )
            )
            discarded = await session.execute(
                delete(Thread).where(Thread.status == "active", Thread.thread_id.not_in(occupied))
            )
        return {
            "interrupted_runs": interrupted.rowcount,
            "archived_threads": archived.rowcount,
            "discarded_threads": discarded.rowcount,
        }

    async def idempotent_response(
        self, user_id: str, client_request_id: str, operation: str
    ) -> dict[str, Any] | None:
        async with self.database.sessions() as session:
            item = await session.get(
                IdempotencyKey,
                {
                    "user_id": user_id,
                    "client_request_id": client_request_id,
                    "operation": operation,
                },
            )
            return dict(item.response_json) if item else None

    async def active_thread(self, user_id: str) -> dict[str, Any] | None:
        async with self.database.sessions() as session:
            thread = await session.scalar(
                select(Thread).where(Thread.user_id == user_id, Thread.status == "active")
            )
            if thread is None:
                return None
            result = _thread_dict(thread)
            result["message_count"] = await session.scalar(
                select(func.count(Message.message_id)).where(Message.thread_id == thread.thread_id)
            )
            result["last_run_status"] = (
                await session.scalar(select(Run.status).where(Run.run_id == thread.last_run_id))
                if thread.last_run_id
                else None
            )
            return result

    async def list_threads(
        self,
        user_id: str,
        *,
        status: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        if cursor and status != "archived":
            raise ApiError(422, "INVALID_CURSOR", "活动会话列表不使用归档游标")
        statement = select(Thread).where(Thread.user_id == user_id, Thread.status == status)
        if cursor:
            archived_at, thread_id = _decode_cursor(cursor)
            statement = statement.where(
                (Thread.archived_at < archived_at)
                | ((Thread.archived_at == archived_at) & (Thread.thread_id < thread_id))
            )
        if status == "archived":
            statement = statement.order_by(Thread.archived_at.desc(), Thread.thread_id.desc())
        else:
            statement = statement.order_by(Thread.updated_at.desc(), Thread.thread_id.desc())
        async with self.database.sessions() as session:
            threads = list((await session.scalars(statement.limit(limit + 1))).all())
            items: list[dict[str, Any]] = []
            for thread in threads[:limit]:
                item = _thread_dict(thread)
                item["message_count"] = await session.scalar(
                    select(func.count(Message.message_id)).where(
                        Message.thread_id == thread.thread_id
                    )
                )
                item["last_run_status"] = (
                    await session.scalar(select(Run.status).where(Run.run_id == thread.last_run_id))
                    if thread.last_run_id
                    else None
                )
                items.append(item)
        next_cursor = None
        if len(threads) > limit and items and status == "archived":
            last = items[-1]
            next_cursor = _encode_cursor(last["archived_at"], last["thread_id"])
        return {"items": items, "next_cursor": next_cursor}

    async def replace_thread(
        self,
        *,
        user_id: str,
        current_thread_id: str | None,
        client_request_id: str,
        new_thread_id: str,
    ) -> dict[str, Any]:
        now = _now()
        async with self.database.sessions.begin() as session:
            user = await session.get(User, user_id, with_for_update=True)
            if user is None:
                raise ApiError(404, "USER_NOT_FOUND", "当前用户不存在")
            idem = await session.get(
                IdempotencyKey,
                {
                    "user_id": user_id,
                    "client_request_id": client_request_id,
                    "operation": "create_thread",
                },
            )
            if idem:
                return dict(idem.response_json)
            active = await session.scalar(
                select(Thread)
                .where(Thread.user_id == user_id, Thread.status == "active")
                .with_for_update()
            )
            if active and active.thread_id != current_thread_id:
                raise ApiError(
                    409,
                    "ACTIVE_THREAD_CHANGED",
                    "活动会话已被其他请求替换",
                    details={"active_thread": _thread_dict(active)},
                )
            if not active and current_thread_id is not None:
                raise ApiError(
                    409,
                    "ACTIVE_THREAD_CHANGED",
                    "指定活动会话已经不存在",
                    details={"active_thread": None},
                )
            archived_thread_id = None
            archived_run_id = None
            if active:
                running = await session.scalar(
                    select(Run).where(
                        Run.thread_id == active.thread_id,
                        Run.status.in_(("starting", "running", "cancelling")),
                    )
                )
                if running:
                    raise ApiError(
                        409,
                        "RUN_CANCELLATION_FAILED",
                        "活动任务尚未终结，不能归档会话",
                        retryable=True,
                        details={"run_id": running.run_id, "status": running.status},
                    )
                run_count = await session.scalar(
                    select(func.count(Run.run_id)).where(Run.thread_id == active.thread_id)
                )
                message_count = await session.scalar(
                    select(func.count(Message.message_id)).where(
                        Message.thread_id == active.thread_id
                    )
                )
                if run_count or message_count:
                    archived_thread_id = active.thread_id
                    archived_run_id = active.last_run_id
                    active.status = "archived"
                    active.active_slot = None
                    active.archived_at = now
                    active.updated_at = now
                    active.archive_reason = "new_thread"
                else:
                    await session.delete(active)
                    await session.flush()
            thread = Thread(
                thread_id=new_thread_id,
                user_id=user_id,
                title="新对话",
                status="active",
                active_slot=1,
                created_at=now,
                updated_at=now,
            )
            session.add(thread)
            response = {
                "thread_id": new_thread_id,
                "status": "active",
                "title": "新对话",
                "created_at": _iso(now),
                "archived_thread_id": archived_thread_id,
                "archived_run_id": archived_run_id,
            }
            session.add(
                IdempotencyKey(
                    user_id=user_id,
                    client_request_id=client_request_id,
                    operation="create_thread",
                    response_json=response,
                    response_status=201,
                    created_at=now,
                )
            )
        return response

    async def create_run(
        self,
        *,
        user_id: str,
        thread_id: str,
        run_id: str,
        query: str,
        client_request_id: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        now = datetime.fromisoformat(response["created_at"].replace("Z", "+00:00")).replace(
            tzinfo=None
        )
        async with self.database.sessions.begin() as session:
            user = await session.get(User, user_id, with_for_update=True)
            if user is None:
                raise ApiError(404, "USER_NOT_FOUND", "当前用户不存在")
            idem = await session.get(
                IdempotencyKey,
                {
                    "user_id": user_id,
                    "client_request_id": client_request_id,
                    "operation": "create_run",
                },
            )
            if idem:
                return dict(idem.response_json)
            thread = await session.scalar(
                select(Thread)
                .where(Thread.thread_id == thread_id, Thread.user_id == user_id)
                .with_for_update()
            )
            if thread is None:
                raise ApiError(404, "THREAD_NOT_FOUND", "指定会话不存在")
            if thread.status != "active":
                raise ApiError(409, "THREAD_ARCHIVED", "归档会话为只读，不能创建任务")
            running = await session.scalar(
                select(Run.run_id).where(
                    Run.thread_id == thread_id,
                    Run.status.in_(("starting", "running", "cancelling")),
                )
            )
            if running:
                raise ApiError(
                    409,
                    "RUN_REPLACEMENT_TIMEOUT",
                    "旧任务尚未完成清理，不能创建新任务",
                    retryable=True,
                    details={"run_id": running},
                )
            ordinal = (
                await session.scalar(
                    select(func.coalesce(func.max(Message.ordinal), 0) + 1).where(
                        Message.thread_id == thread_id
                    )
                )
                or 1
            )
            run = Run(
                run_id=run_id,
                thread_id=thread_id,
                status="starting",
                query=query,
                attempt=1,
                created_at=now,
            )
            session.add(run)
            # Message.run_id references this row.  These models deliberately do
            # not expose ORM relationships, so make the parent INSERT ordering
            # explicit for MySQL instead of relying on a combined flush.
            await session.flush()
            session.add(
                Message(
                    message_id=uuid4().hex,
                    thread_id=thread_id,
                    run_id=run_id,
                    role="user",
                    content=query,
                    is_partial=False,
                    ordinal=ordinal,
                    created_at=now,
                )
            )
            if thread.title == "新对话":
                thread.title = _title(query)
            thread.updated_at = now
            thread.last_run_id = run_id
            session.add(
                IdempotencyKey(
                    user_id=user_id,
                    client_request_id=client_request_id,
                    operation="create_run",
                    response_json=response,
                    response_status=202,
                    created_at=now,
                )
            )
        return response

    async def set_run_status(
        self,
        thread_id: str,
        run_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = _now()
        values: dict[str, Any] = {"status": status}
        if status == "running":
            values["started_at"] = now
        if status in TERMINAL_RUN_STATUSES:
            values["finished_at"] = now
        if error_code is not None:
            values["error_code"] = error_code
        if error_message is not None:
            values["error_message"] = error_message
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                update(Run).where(Run.thread_id == thread_id, Run.run_id == run_id).values(**values)
            )
        if result.rowcount != 1:
            raise ApiError(404, "RUN_NOT_FOUND", "指定任务不存在或已经过期")

    async def finish_run(
        self,
        *,
        thread_id: str,
        run_id: str,
        status: str,
        final_text: str,
        result: dict[str, Any] | None,
        message_id: str,
        is_partial: bool,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = _now()
        async with self.database.sessions.begin() as session:
            run = await session.scalar(
                select(Run)
                .where(Run.thread_id == thread_id, Run.run_id == run_id)
                .with_for_update()
            )
            if run is None:
                raise ApiError(404, "RUN_NOT_FOUND", "指定任务不存在或已经过期")
            if run.status in TERMINAL_RUN_STATUSES:
                return
            run.status = status
            run.finished_at = now
            run.error_code = error_code
            run.error_message = error_message
            session.add(
                RunResult(
                    run_id=run_id,
                    final_text=final_text or None,
                    result_json=result,
                    metadata_json=None,
                    completed_at=now,
                )
            )
            if final_text:
                ordinal = (
                    await session.scalar(
                        select(func.coalesce(func.max(Message.ordinal), 0) + 1).where(
                            Message.thread_id == thread_id
                        )
                    )
                    or 1
                )
                session.add(
                    Message(
                        message_id=message_id,
                        thread_id=thread_id,
                        run_id=run_id,
                        role="assistant",
                        content=final_text,
                        is_partial=is_partial,
                        ordinal=ordinal,
                        created_at=now,
                    )
                )
            await session.execute(
                update(Thread).where(Thread.thread_id == thread_id).values(updated_at=now)
            )

    async def get_run(
        self, thread_id: str, run_id: str, *, user_id: str | None = None
    ) -> dict[str, Any]:
        statement = (
            select(Run, Thread, RunResult)
            .join(Thread, Thread.thread_id == Run.thread_id)
            .outerjoin(RunResult, RunResult.run_id == Run.run_id)
            .where(Run.thread_id == thread_id, Run.run_id == run_id)
        )
        if user_id is not None:
            statement = statement.where(Thread.user_id == user_id)
        async with self.database.sessions() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            raise ApiError(404, "RUN_NOT_FOUND", "指定任务不存在或已经过期")
        run, thread, run_result = row
        return {
            "run_id": run.run_id,
            "thread_id": run.thread_id,
            "status": run.status,
            "query": run.query,
            "created_at": _iso(run.created_at),
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
            "final_text": run_result.final_text if run_result else None,
            "result": run_result.result_json if run_result else None,
            "error_code": run.error_code,
            "error_message": run.error_message,
            "thread_status": thread.status,
            "user_id": thread.user_id,
        }

    async def thread_detail(self, thread_id: str, user_id: str) -> dict[str, Any]:
        async with self.database.sessions() as session:
            thread = await session.scalar(
                select(Thread).where(Thread.thread_id == thread_id, Thread.user_id == user_id)
            )
            if thread is None:
                raise ApiError(404, "THREAD_NOT_FOUND", "指定会话不存在")
            messages = list(
                (
                    await session.scalars(
                        select(Message)
                        .where(Message.thread_id == thread_id)
                        .order_by(Message.ordinal)
                    )
                ).all()
            )
            run_rows = list(
                (
                    await session.execute(
                        select(Run, RunResult)
                        .outerjoin(RunResult, RunResult.run_id == Run.run_id)
                        .where(Run.thread_id == thread_id)
                        .order_by(Run.created_at, Run.run_id)
                    )
                ).all()
            )
            artifacts = list(
                (
                    await session.scalars(
                        select(Artifact)
                        .where(Artifact.thread_id == thread_id)
                        .order_by(Artifact.created_at, Artifact.file_id)
                    )
                ).all()
            )
        runs = []
        for run, run_result in run_rows:
            runs.append(
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "query": run.query,
                    "created_at": _iso(run.created_at),
                    "started_at": _iso(run.started_at),
                    "finished_at": _iso(run.finished_at),
                    "final_text": run_result.final_text if run_result else None,
                    "result": run_result.result_json if run_result else None,
                    "error_code": run.error_code,
                    "error_message": run.error_message,
                    "artifacts": [
                        _artifact_dict(item) for item in artifacts if item.run_id == run.run_id
                    ],
                }
            )
        result = _thread_dict(thread)
        result.update(
            {
                "read_only": thread.status == "archived",
                "messages": [
                    {
                        "message_id": item.message_id,
                        "run_id": item.run_id,
                        "role": item.role,
                        "content": item.content,
                        "is_partial": item.is_partial,
                        "ordinal": item.ordinal,
                        "created_at": _iso(item.created_at),
                    }
                    for item in messages
                ],
                "runs": runs,
            }
        )
        return result

    async def list_artifacts(self, thread_id: str, run_id: str) -> list[dict[str, Any]]:
        await self.get_run(thread_id, run_id)
        async with self.database.sessions() as session:
            items = list(
                (
                    await session.scalars(
                        select(Artifact)
                        .where(Artifact.thread_id == thread_id, Artifact.run_id == run_id)
                        .order_by(Artifact.created_at, Artifact.file_id)
                    )
                ).all()
            )
        return [_artifact_dict(item) for item in items]

    async def artifact(self, thread_id: str, run_id: str, file_id: str) -> dict[str, Any]:
        async with self.database.sessions() as session:
            item = await session.scalar(
                select(Artifact).where(
                    Artifact.thread_id == thread_id,
                    Artifact.run_id == run_id,
                    Artifact.file_id == file_id,
                )
            )
        if item is None:
            raise ApiError(404, "FILE_NOT_FOUND", "指定产物不存在")
        return _artifact_dict(item)

    async def register_artifact(
        self,
        *,
        thread_id: str,
        run_id: str,
        filename: str,
        kind: str,
        media_type: str,
        size: int,
        relative_path: str,
        file_id: str | None = None,
    ) -> dict[str, Any]:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ApiError(422, "INVALID_ARTIFACT_PATH", "产物相对路径无效")
        run = await self.get_run(thread_id, run_id)
        if run["thread_status"] != "active":
            raise ApiError(409, "THREAD_ARCHIVED", "归档会话为只读，不能注册产物")
        item = Artifact(
            file_id=file_id or uuid4().hex,
            thread_id=thread_id,
            run_id=run_id,
            filename=filename,
            kind=kind,
            media_type=media_type,
            size=size,
            relative_path=relative_path,
            created_at=_now(),
        )
        try:
            async with self.database.sessions.begin() as session:
                session.add(item)
        except IntegrityError as exc:
            raise ApiError(409, "ARTIFACT_EXISTS", "产物记录已经存在") from exc
        return _artifact_dict(item)
