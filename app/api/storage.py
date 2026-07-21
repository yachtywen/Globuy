"""SQLite persistence for sessions, runs, messages, and artifact manifests."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.api.errors import ApiError

TERMINAL_RUN_STATUSES = frozenset(
    {"succeeded", "cancelled", "failed", "interrupted"}
)
ACTIVE_RUN_STATUSES = frozenset({"starting", "running", "cancelling"})


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _title(query: str) -> str:
    return " ".join(query.split())[:32] or "新对话"


def _encode_cursor(timestamp: str, thread_id: str) -> str:
    raw = json.dumps([timestamp, thread_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        return str(value[0]), str(value[1])
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApiError(422, "INVALID_CURSOR", "归档游标无效") from exc


class SessionStore:
    """One WAL-enabled application connection, serialized at transaction boundaries."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    def _db(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("SessionStore 尚未启动")
        return self.connection

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA foreign_keys=ON")
        await self.connection.execute("PRAGMA busy_timeout=5000")
        await self._migrate()

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    async def _migrate(self) -> None:
        db = self._db()
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS threads (
                thread_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '新对话',
                status TEXT NOT NULL CHECK(status IN ('active','archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT,
                archive_reason TEXT,
                last_run_id TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_thread_per_user
                ON threads(user_id) WHERE status='active';
            CREATE INDEX IF NOT EXISTS archived_threads_order
                ON threads(user_id, archived_at DESC, thread_id DESC)
                WHERE status='archived';

            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN (
                    'starting','running','cancelling','succeeded','cancelled','failed','interrupted'
                )),
                query TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                final_text TEXT,
                result_json TEXT,
                error_code TEXT,
                error_message TEXT
            );
            CREATE INDEX IF NOT EXISTS runs_by_thread ON runs(thread_id, created_at, run_id);

            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                content TEXT NOT NULL,
                is_partial INTEGER NOT NULL DEFAULT 0 CHECK(is_partial IN (0,1)),
                ordinal INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(thread_id, ordinal)
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                file_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                kind TEXT NOT NULL,
                media_type TEXT NOT NULL,
                size INTEGER NOT NULL CHECK(size >= 0),
                relative_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS artifacts_by_run ON artifacts(thread_id, run_id, created_at);

            CREATE TABLE IF NOT EXISTS idempotency_keys (
                user_id TEXT NOT NULL,
                client_request_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, client_request_id, operation)
            );
            PRAGMA user_version=1;
            """
        )
        await db.commit()

    async def recover_after_restart(self) -> dict[str, int]:
        """Make process-local runs truthful after a server restart."""

        db = self._db()
        now = utc_now()
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                interrupted = await db.execute(
                    """
                    UPDATE runs
                    SET status='interrupted', finished_at=?, error_code='SERVER_RESTART',
                        error_message='服务重启导致任务中断'
                    WHERE status IN ('starting','running','cancelling')
                    """,
                    (now,),
                )
                archived = await db.execute(
                    """
                    UPDATE threads
                    SET status='archived', archived_at=?, updated_at=?,
                        archive_reason='server_restart'
                    WHERE status='active' AND (
                        EXISTS(SELECT 1 FROM runs r WHERE r.thread_id=threads.thread_id)
                        OR EXISTS(SELECT 1 FROM messages m WHERE m.thread_id=threads.thread_id)
                    )
                    """,
                    (now, now),
                )
                discarded = await db.execute(
                    """
                    DELETE FROM threads
                    WHERE status='active'
                      AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.thread_id=threads.thread_id)
                      AND NOT EXISTS(SELECT 1 FROM messages m WHERE m.thread_id=threads.thread_id)
                    """
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "interrupted_runs": interrupted.rowcount,
            "archived_threads": archived.rowcount,
            "discarded_threads": discarded.rowcount,
        }

    async def idempotent_response(
        self, user_id: str, client_request_id: str, operation: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._db().execute(
                """
                SELECT response_json FROM idempotency_keys
                WHERE user_id=? AND client_request_id=? AND operation=?
                """,
                (user_id, client_request_id, operation),
            )
            row = await cursor.fetchone()
        return _json_load(row["response_json"], None) if row else None

    async def active_thread(self, user_id: str) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._db().execute(
                """
                SELECT t.*,
                       (SELECT status FROM runs r WHERE r.run_id=t.last_run_id) last_run_status,
                       (SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.thread_id) message_count
                FROM threads t WHERE user_id=? AND status='active'
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_threads(
        self,
        user_id: str,
        *,
        status: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        params: list[Any] = [user_id, status]
        clause = ""
        if cursor:
            if status != "archived":
                raise ApiError(422, "INVALID_CURSOR", "活动会话列表不使用归档游标")
            archived_at, thread_id = _decode_cursor(cursor)
            clause = " AND (t.archived_at < ? OR (t.archived_at = ? AND t.thread_id < ?))"
            params.extend([archived_at, archived_at, thread_id])
        params.append(limit + 1)
        order = (
            "t.archived_at DESC, t.thread_id DESC"
            if status == "archived"
            else "t.updated_at DESC, t.thread_id DESC"
        )
        async with self._lock:
            db_cursor = await self._db().execute(
                f"""
                SELECT t.thread_id,t.title,t.status,t.created_at,t.updated_at,t.archived_at,
                       (SELECT status FROM runs r WHERE r.run_id=t.last_run_id) last_run_status,
                       (SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.thread_id) message_count
                FROM threads t
                WHERE t.user_id=? AND t.status=? {clause}
                ORDER BY {order}
                LIMIT ?
                """,  # nosec B608: only fixed clauses are interpolated.
                params,
            )
            rows = [dict(row) for row in await db_cursor.fetchall()]
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = None
        if has_more and items and status == "archived":
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
        db = self._db()
        now = utc_now()
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                idem_cursor = await db.execute(
                    """
                    SELECT response_json FROM idempotency_keys
                    WHERE user_id=? AND client_request_id=? AND operation='create_thread'
                    """,
                    (user_id, client_request_id),
                )
                idem = await idem_cursor.fetchone()
                if idem:
                    await db.rollback()
                    return _json_load(idem["response_json"], {})

                active_cursor = await db.execute(
                    "SELECT * FROM threads WHERE user_id=? AND status='active'",
                    (user_id,),
                )
                active = await active_cursor.fetchone()
                if active and active["thread_id"] != current_thread_id:
                    raise ApiError(
                        409,
                        "ACTIVE_THREAD_CHANGED",
                        "活动会话已被其他请求替换",
                        details={"active_thread": dict(active)},
                    )
                if not active and current_thread_id is not None:
                    raise ApiError(
                        409,
                        "ACTIVE_THREAD_CHANGED",
                        "指定活动会话已不存在",
                        details={"active_thread": None},
                    )

                archived_thread_id: str | None = None
                archived_run_id: str | None = None
                if active:
                    active_run_cursor = await db.execute(
                        """
                        SELECT run_id,status FROM runs
                        WHERE thread_id=? AND status IN ('starting','running','cancelling')
                        LIMIT 1
                        """,
                        (active["thread_id"],),
                    )
                    active_run = await active_run_cursor.fetchone()
                    if active_run:
                        raise ApiError(
                            409,
                            "RUN_CANCELLATION_FAILED",
                            "活动任务尚未终结，不能归档会话",
                            retryable=True,
                            details={
                                "run_id": active_run["run_id"],
                                "status": active_run["status"],
                            },
                        )
                    count_cursor = await db.execute(
                        """
                        SELECT
                          (SELECT COUNT(*) FROM runs WHERE thread_id=?) run_count,
                          (SELECT COUNT(*) FROM messages WHERE thread_id=?) message_count
                        """,
                        (active["thread_id"], active["thread_id"]),
                    )
                    counts = await count_cursor.fetchone()
                    if counts["run_count"] or counts["message_count"]:
                        archived_thread_id = active["thread_id"]
                        archived_run_id = active["last_run_id"]
                        await db.execute(
                            """
                            UPDATE threads SET status='archived',archived_at=?,updated_at=?,
                                               archive_reason='new_thread'
                            WHERE thread_id=? AND status='active'
                            """,
                            (now, now, active["thread_id"]),
                        )
                    else:
                        await db.execute(
                            "DELETE FROM threads WHERE thread_id=?",
                            (active["thread_id"],),
                        )

                await db.execute(
                    """
                    INSERT INTO threads(
                        thread_id,user_id,title,status,created_at,updated_at
                    ) VALUES(?,?,?,'active',?,?)
                    """,
                    (new_thread_id, user_id, "新对话", now, now),
                )
                response = {
                    "thread_id": new_thread_id,
                    "status": "active",
                    "title": "新对话",
                    "created_at": now,
                    "archived_thread_id": archived_thread_id,
                    "archived_run_id": archived_run_id,
                }
                await db.execute(
                    """
                    INSERT INTO idempotency_keys(
                        user_id,client_request_id,operation,response_json,created_at
                    ) VALUES(?,?,'create_thread',?,?)
                    """,
                    (user_id, client_request_id, json.dumps(response), now),
                )
                await db.commit()
                return response
            except BaseException:
                await db.rollback()
                raise

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
        db = self._db()
        now = response["created_at"]
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                idem_cursor = await db.execute(
                    """
                    SELECT response_json FROM idempotency_keys
                    WHERE user_id=? AND client_request_id=? AND operation='create_run'
                    """,
                    (user_id, client_request_id),
                )
                idem = await idem_cursor.fetchone()
                if idem:
                    await db.rollback()
                    return _json_load(idem["response_json"], {})
                thread_cursor = await db.execute(
                    "SELECT * FROM threads WHERE thread_id=? AND user_id=?",
                    (thread_id, user_id),
                )
                thread = await thread_cursor.fetchone()
                if not thread:
                    raise ApiError(404, "THREAD_NOT_FOUND", "指定会话不存在")
                if thread["status"] != "active":
                    raise ApiError(409, "THREAD_ARCHIVED", "归档会话为只读，不能创建任务")
                active_cursor = await db.execute(
                    "SELECT thread_id FROM threads WHERE user_id=? AND status='active'",
                    (user_id,),
                )
                active = await active_cursor.fetchone()
                if not active or active["thread_id"] != thread_id:
                    raise ApiError(409, "ACTIVE_THREAD_CHANGED", "当前活动会话已经改变")
                active_run_cursor = await db.execute(
                    """
                    SELECT run_id FROM runs
                    WHERE thread_id=? AND status IN ('starting','running','cancelling')
                    LIMIT 1
                    """,
                    (thread_id,),
                )
                active_run = await active_run_cursor.fetchone()
                if active_run:
                    raise ApiError(
                        409,
                        "RUN_REPLACEMENT_TIMEOUT",
                        "旧任务尚未完成清理，不能创建新任务",
                        retryable=True,
                        details={"run_id": active_run["run_id"]},
                    )
                ordinal_cursor = await db.execute(
                    """
                    SELECT COALESCE(MAX(ordinal),0)+1 next_ordinal
                    FROM messages WHERE thread_id=?
                    """,
                    (thread_id,),
                )
                ordinal = (await ordinal_cursor.fetchone())["next_ordinal"]
                await db.execute(
                    """
                    INSERT INTO runs(run_id,thread_id,status,query,created_at)
                    VALUES(?,?,'starting',?,?)
                    """,
                    (run_id, thread_id, query, now),
                )
                await db.execute(
                    """
                    INSERT INTO messages(
                        message_id,thread_id,run_id,role,content,is_partial,ordinal,created_at
                    ) VALUES(?,?,?,'user',?,0,?,?)
                    """,
                    (uuid4().hex, thread_id, run_id, query, ordinal, now),
                )
                title = thread["title"]
                if title == "新对话":
                    title = _title(query)
                await db.execute(
                    """
                    UPDATE threads SET title=?,updated_at=?,last_run_id=? WHERE thread_id=?
                    """,
                    (title, now, run_id, thread_id),
                )
                await db.execute(
                    """
                    INSERT INTO idempotency_keys(
                        user_id,client_request_id,operation,response_json,created_at
                    ) VALUES(?,?,'create_run',?,?)
                    """,
                    (user_id, client_request_id, json.dumps(response), now),
                )
                await db.commit()
                return response
            except BaseException:
                await db.rollback()
                raise

    async def set_run_status(
        self,
        thread_id: str,
        run_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = utc_now()
        started_at = now if status == "running" else None
        finished_at = now if status in TERMINAL_RUN_STATUSES else None
        async with self._lock:
            cursor = await self._db().execute(
                """
                UPDATE runs SET status=?,
                    started_at=COALESCE(started_at,?),
                    finished_at=COALESCE(?,finished_at),
                    error_code=COALESCE(?,error_code),
                    error_message=COALESCE(?,error_message)
                WHERE thread_id=? AND run_id=?
                """,
                (
                    status,
                    started_at,
                    finished_at,
                    error_code,
                    error_message,
                    thread_id,
                    run_id,
                ),
            )
            await self._db().commit()
        if cursor.rowcount != 1:
            raise ApiError(404, "RUN_NOT_FOUND", "指定任务不存在或已过期")

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
        db = self._db()
        now = utc_now()
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT status FROM runs WHERE thread_id=? AND run_id=?",
                    (thread_id, run_id),
                )
                row = await cursor.fetchone()
                if not row:
                    raise ApiError(404, "RUN_NOT_FOUND", "指定任务不存在或已过期")
                if row["status"] in TERMINAL_RUN_STATUSES:
                    await db.rollback()
                    return
                await db.execute(
                    """
                    UPDATE runs SET status=?,finished_at=?,final_text=?,result_json=?,
                                    error_code=?,error_message=?
                    WHERE thread_id=? AND run_id=?
                    """,
                    (
                        status,
                        now,
                        final_text or None,
                        json.dumps(result, ensure_ascii=False) if result is not None else None,
                        error_code,
                        error_message,
                        thread_id,
                        run_id,
                    ),
                )
                if final_text:
                    ordinal_cursor = await db.execute(
                        """
                        SELECT COALESCE(MAX(ordinal),0)+1 next_ordinal
                        FROM messages WHERE thread_id=?
                        """,
                        (thread_id,),
                    )
                    ordinal = (await ordinal_cursor.fetchone())["next_ordinal"]
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO messages(
                            message_id,thread_id,run_id,role,content,is_partial,ordinal,created_at
                        ) VALUES(?,?,?,'assistant',?,?,?,?)
                        """,
                        (
                            message_id,
                            thread_id,
                            run_id,
                            final_text,
                            int(is_partial),
                            ordinal,
                            now,
                        ),
                    )
                await db.execute(
                    "UPDATE threads SET updated_at=? WHERE thread_id=?",
                    (now, thread_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def get_run(
        self, thread_id: str, run_id: str, *, user_id: str | None = None
    ) -> dict[str, Any]:
        params: list[Any] = [thread_id, run_id]
        owner_clause = ""
        if user_id is not None:
            owner_clause = " AND t.user_id=?"
            params.append(user_id)
        async with self._lock:
            cursor = await self._db().execute(
                f"""
                SELECT r.*,t.status thread_status,t.user_id
                FROM runs r JOIN threads t ON t.thread_id=r.thread_id
                WHERE r.thread_id=? AND r.run_id=? {owner_clause}
                """,  # nosec B608: owner_clause is fixed.
                params,
            )
            row = await cursor.fetchone()
        if not row:
            raise ApiError(404, "RUN_NOT_FOUND", "指定任务不存在或已过期")
        result = dict(row)
        result["result"] = _json_load(result.pop("result_json"), None)
        return result

    async def thread_detail(self, thread_id: str, user_id: str) -> dict[str, Any]:
        async with self._lock:
            thread_cursor = await self._db().execute(
                "SELECT * FROM threads WHERE thread_id=? AND user_id=?",
                (thread_id, user_id),
            )
            thread = await thread_cursor.fetchone()
            if not thread:
                raise ApiError(404, "THREAD_NOT_FOUND", "指定会话不存在")
            messages_cursor = await self._db().execute(
                """
                SELECT message_id,run_id,role,content,is_partial,ordinal,created_at
                FROM messages WHERE thread_id=? ORDER BY ordinal
                """,
                (thread_id,),
            )
            runs_cursor = await self._db().execute(
                """
                SELECT run_id,status,query,created_at,started_at,finished_at,final_text,
                       result_json,error_code,error_message
                FROM runs WHERE thread_id=? ORDER BY created_at,run_id
                """,
                (thread_id,),
            )
            artifacts_cursor = await self._db().execute(
                """
                SELECT file_id,run_id,filename,kind,media_type,size,created_at
                FROM artifacts WHERE thread_id=? ORDER BY created_at,file_id
                """,
                (thread_id,),
            )
            messages = [dict(row) for row in await messages_cursor.fetchall()]
            runs = [dict(row) for row in await runs_cursor.fetchall()]
            artifacts = [dict(row) for row in await artifacts_cursor.fetchall()]
        for item in messages:
            item["is_partial"] = bool(item["is_partial"])
        for item in runs:
            item["result"] = _json_load(item.pop("result_json"), None)
            item["artifacts"] = [a for a in artifacts if a["run_id"] == item["run_id"]]
        result = dict(thread)
        result.update(
            {
                "read_only": thread["status"] == "archived",
                "messages": messages,
                "runs": runs,
            }
        )
        return result

    async def list_artifacts(self, thread_id: str, run_id: str) -> list[dict[str, Any]]:
        await self.get_run(thread_id, run_id)
        async with self._lock:
            cursor = await self._db().execute(
                """
                SELECT * FROM artifacts WHERE thread_id=? AND run_id=?
                ORDER BY created_at,file_id
                """,
                (thread_id, run_id),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def artifact(
        self, thread_id: str, run_id: str, file_id: str
    ) -> dict[str, Any]:
        async with self._lock:
            cursor = await self._db().execute(
                """
                SELECT * FROM artifacts
                WHERE thread_id=? AND run_id=? AND file_id=?
                """,
                (thread_id, run_id, file_id),
            )
            row = await cursor.fetchone()
        if not row:
            raise ApiError(404, "FILE_NOT_FOUND", "指定产物不存在")
        return dict(row)

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
        """Register a verified real file; callers remain responsible for path validation."""

        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ApiError(422, "INVALID_ARTIFACT_PATH", "产物相对路径无效")
        run = await self.get_run(thread_id, run_id)
        if run["thread_status"] != "active":
            raise ApiError(409, "THREAD_ARCHIVED", "归档会话为只读，不能注册产物")
        item = {
            "file_id": file_id or uuid4().hex,
            "thread_id": thread_id,
            "run_id": run_id,
            "filename": filename,
            "kind": kind,
            "media_type": media_type,
            "size": size,
            "relative_path": relative_path,
            "created_at": utc_now(),
        }
        async with self._lock:
            await self._db().execute(
                """
                INSERT INTO artifacts(
                    file_id,thread_id,run_id,filename,kind,media_type,size,relative_path,created_at
                ) VALUES(:file_id,:thread_id,:run_id,:filename,:kind,:media_type,:size,
                         :relative_path,:created_at)
                """,
                item,
            )
            await self._db().commit()
        return item
