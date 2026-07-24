"""Background Agent run ownership, cancellation, and durable terminal handling."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.api.context import bind_context
from app.api.errors import ApiError
from app.api.event_broker import EventBroker
from app.api.monitor import EventType, Monitor, monitor_scope
from app.api.storage import TERMINAL_RUN_STATUSES, SessionStore, utc_now
from app.presentation import sanitize_shopping_markdown, visible_unresolved
from app.search.catalog_images import enrich_task_result

AgentRunner = Callable[[str, str], Awaitable[tuple[str, dict[str, Any]]]]
AgentStreamRunner = Callable[[str, str], AsyncIterator[dict[str, Any]]]

logger = logging.getLogger(__name__)


def _merged_preferences(*groups: object) -> list[dict[str, Any]]:
    """Preserve deterministic candidates when a terminal tool returns an empty list."""

    result: list[dict[str, Any]] = []
    keys: set[str] = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for value in group:
            if not isinstance(value, dict):
                continue
            key = str(value.get("key") or "").strip()
            if key and key not in keys:
                result.append(value)
                keys.add(key)
    return result


@dataclass
class RunHandle:
    thread_id: str
    run_id: str
    user_id: str
    query: str
    task: asyncio.Task[None] | None = None
    started_event: asyncio.Event = field(default_factory=asyncio.Event)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "starting"


class RunRegistry:
    def __init__(
        self,
        *,
        store: SessionStore,
        broker: EventBroker,
        agent_runner: AgentRunner,
        stream_runner: AgentStreamRunner | None,
        session_dir: Callable[[str], Path],
        product_image_catalog_path: Path,
        cancel_grace_seconds: float = 5.0,
    ) -> None:
        self.store = store
        self.broker = broker
        self.agent_runner = agent_runner
        self.stream_runner = stream_runner
        self.session_dir = session_dir
        self.product_image_catalog_path = product_image_catalog_path
        self.cancel_grace_seconds = cancel_grace_seconds
        self.active_tasks: dict[str, RunHandle] = {}
        self.thread_locks: dict[str, asyncio.Lock] = {}
        self.user_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._closing = False

    async def _thread_lock(self, thread_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self.thread_locks.setdefault(thread_id, asyncio.Lock())

    async def _user_lock(self, user_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self.user_locks.setdefault(user_id, asyncio.Lock())

    async def start_run(
        self,
        *,
        query: str,
        thread_id: str,
        user_id: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        if self._closing:
            raise ApiError(503, "SERVICE_SHUTTING_DOWN", "服务正在关闭", retryable=True)
        user_lock = await self._user_lock(user_id)
        async with user_lock:
            idem = await self.store.idempotent_response(
                user_id, client_request_id, "create_run"
            )
            if idem is not None:
                return idem
            thread_lock = await self._thread_lock(thread_id)
            async with thread_lock:
                replaced_run_id: str | None = None
                previous = self.active_tasks.get(thread_id)
                if previous is not None:
                    replaced_run_id = previous.run_id
                    await self._cancel_and_wait(previous)

                run_id = uuid4().hex
                created_at = utc_now()
                response = {
                    "status": "starting",
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "replaced_run_id": replaced_run_id,
                    "created_at": created_at,
                    "ws_url": f"/api/v1/ws/{thread_id}?run_id={run_id}&after=0",
                    "status_url": f"/api/v1/threads/{thread_id}/runs/{run_id}",
                }
                stored = await self.store.create_run(
                    user_id=user_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    query=query,
                    client_request_id=client_request_id,
                    response=response,
                )
                if stored.get("run_id") != run_id:
                    return stored
                await self.broker.ensure_stream(thread_id, run_id)
                handle = RunHandle(thread_id, run_id, user_id, query)
                self.active_tasks[thread_id] = handle
                handle.task = asyncio.create_task(
                    self._execute(handle), name=f"globuy-run-{run_id}"
                )
                await handle.started_event.wait()
                return response

    async def create_thread(
        self,
        *,
        user_id: str,
        current_thread_id: str | None,
        client_request_id: str,
    ) -> dict[str, Any]:
        user_lock = await self._user_lock(user_id)
        async with user_lock:
            idem = await self.store.idempotent_response(
                user_id, client_request_id, "create_thread"
            )
            if idem is not None:
                return idem
            active = await self.store.active_thread(user_id)
            if active and active["thread_id"] != current_thread_id:
                raise ApiError(
                    409,
                    "ACTIVE_THREAD_CHANGED",
                    "活动会话已被其他请求替换",
                    details={"active_thread": active},
                )
            if not active and current_thread_id is not None:
                raise ApiError(
                    409,
                    "ACTIVE_THREAD_CHANGED",
                    "指定活动会话已不存在",
                    details={"active_thread": None},
                )
            if active:
                thread_lock = await self._thread_lock(active["thread_id"])
                async with thread_lock:
                    handle = self.active_tasks.get(active["thread_id"])
                    if handle is not None:
                        await self._cancel_and_wait(handle)
            response = await self.store.replace_thread(
                user_id=user_id,
                current_thread_id=current_thread_id,
                client_request_id=client_request_id,
                new_thread_id=uuid4().hex,
            )
            archived_thread = response.get("archived_thread_id")
            archived_run = response.get("archived_run_id")
            if archived_thread and archived_run:
                await self.broker.publish(
                    EventType.CUSTOM,
                    archived_thread,
                    archived_run,
                    message="当前会话已归档",
                    data={
                        "name": "thread_archived",
                        "thread_id": archived_thread,
                        "new_thread_id": response["thread_id"],
                    },
                )
            return response

    async def cancel_run(self, thread_id: str, run_id: str) -> dict[str, Any]:
        record = await self.store.get_run(thread_id, run_id)
        if record["status"] in TERMINAL_RUN_STATUSES:
            return {
                "status": record["status"],
                "thread_id": thread_id,
                "run_id": run_id,
                "requested_at": utc_now(),
                "terminal": True,
            }
        thread_lock = await self._thread_lock(thread_id)
        async with thread_lock:
            handle = self.active_tasks.get(thread_id)
            if handle is None or handle.run_id != run_id:
                raise ApiError(404, "RUN_NOT_FOUND", "指定任务不存在或已过期")
            if handle.status != "cancelling":
                handle.status = "cancelling"
                await self.store.set_run_status(thread_id, run_id, "cancelling")
                if handle.task is not None:
                    handle.task.cancel()
            return {
                "status": "cancelling",
                "thread_id": thread_id,
                "run_id": run_id,
                "requested_at": utc_now(),
                "terminal": False,
            }

    async def _cancel_and_wait(self, handle: RunHandle) -> None:
        record = await self.store.get_run(handle.thread_id, handle.run_id)
        if record["status"] in TERMINAL_RUN_STATUSES:
            try:
                await asyncio.wait_for(handle.done_event.wait(), self.cancel_grace_seconds)
                if handle.task is not None:
                    await asyncio.gather(handle.task, return_exceptions=True)
                return
            except TimeoutError as exc:
                raise ApiError(
                    409,
                    "RUN_REPLACEMENT_TIMEOUT",
                    "旧任务仍在清理，暂时不能启动新任务",
                    retryable=True,
                    details={"run_id": handle.run_id},
                ) from exc
        if handle.status != "cancelling":
            handle.status = "cancelling"
            await self.store.set_run_status(handle.thread_id, handle.run_id, "cancelling")
            if handle.task is not None:
                handle.task.cancel()
        try:
            await asyncio.wait_for(handle.done_event.wait(), self.cancel_grace_seconds)
        except TimeoutError as exc:
            raise ApiError(
                409,
                "RUN_REPLACEMENT_TIMEOUT",
                "旧任务仍在清理，暂时不能启动新任务",
                retryable=True,
                details={"run_id": handle.run_id},
            ) from exc
        if handle.task is not None:
            await asyncio.gather(handle.task, return_exceptions=True)
        record = await self.store.get_run(handle.thread_id, handle.run_id)
        if record["status"] not in TERMINAL_RUN_STATUSES:
            raise ApiError(
                409,
                "RUN_CANCELLATION_FAILED",
                "旧任务取消未完成，不能继续替换或归档",
                retryable=True,
                details={"run_id": handle.run_id, "status": record["status"]},
            )

    async def run_status(self, thread_id: str, run_id: str) -> dict[str, Any]:
        record = await self.store.get_run(thread_id, run_id)
        cursor = await self.broker.cursor_info(thread_id, run_id)
        artifacts = await self.store.list_artifacts(thread_id, run_id)
        result = enrich_task_result(
            record.get("result"), self.product_image_catalog_path
        )
        return {
            "thread_id": thread_id,
            "run_id": run_id,
            "status": record["status"],
            "created_at": record["created_at"],
            "started_at": record["started_at"],
            "finished_at": record["finished_at"],
            **cursor,
            "result": result,
            "artifacts": [
                self._public_artifact(item, thread_id, run_id) for item in artifacts
            ],
            "memory_status": (
                result.get("memory_status", "not_configured")
                if isinstance(result, dict)
                else "not_configured"
            ),
            "error": (
                {
                    "code": record["error_code"],
                    "message": record["error_message"],
                }
                if record["error_code"]
                else None
            ),
        }

    @staticmethod
    def _public_artifact(
        item: dict[str, Any], thread_id: str, run_id: str
    ) -> dict[str, Any]:
        public = {
            key: item[key]
            for key in ("file_id", "filename", "kind", "media_type", "size", "created_at")
        }
        public["download_url"] = (
            f"/api/v1/threads/{thread_id}/runs/{run_id}/files/{item['file_id']}"
        )
        return public

    async def _execute(self, handle: RunHandle) -> None:
        started = time.perf_counter()
        message_id = uuid4().hex
        deltas: list[str] = []
        metadata: dict[str, Any] = {}
        message_started = False
        handle.started_event.set()
        try:
            handle.status = "running"
            await self.store.set_run_status(handle.thread_id, handle.run_id, "running")
            await self.broker.publish(
                EventType.RUN_STARTED,
                handle.thread_id,
                handle.run_id,
                data={"started_at": utc_now()},
            )
            await self.broker.publish(
                EventType.CUSTOM,
                handle.thread_id,
                handle.run_id,
                message="收到你的消息了~正在初始化本次对话",
                data={"name": "conversation_initializing", "phase": "preparing"},
            )
            await self.broker.publish(
                EventType.TEXT_MESSAGE_START,
                handle.thread_id,
                handle.run_id,
                data={"message_id": message_id, "role": "assistant"},
            )
            message_started = True
            final_state: dict[str, Any] | None = None
            with bind_context(
                handle.thread_id,
                self.session_dir(handle.thread_id),
                run_id=handle.run_id,
                user_id=handle.user_id,
            ), monitor_scope(
                Monitor(self.broker.publish_internal, publish_thread_id=handle.thread_id)
            ):
                if self.stream_runner is None:
                    answer, metadata = await self.agent_runner(handle.query, handle.thread_id)
                    if answer:
                        deltas.append(answer)
                        await self._publish_delta(handle, message_id, answer)
                else:
                    metadata = {"streaming": True}
                    async for graph_event in self.stream_runner(
                        handle.query, handle.thread_id
                    ):
                        if (
                            graph_event.get("event") == "on_chain_end"
                            and not graph_event.get("parent_ids")
                            and isinstance(graph_event.get("data", {}).get("output"), dict)
                        ):
                            final_state = graph_event["data"]["output"]
                        if graph_event.get("event") != "on_chat_model_stream":
                            continue
                        event_metadata = graph_event.get("metadata") or {}
                        if event_metadata.get("model_role") != "shopping_summary":
                            continue
                        chunk = graph_event.get("data", {}).get("chunk")
                        delta = getattr(chunk, "content", "")
                        if isinstance(delta, str) and delta:
                            deltas.append(delta)
                            await self._publish_delta(
                                handle,
                                message_id,
                                delta,
                                model_role="shopping_summary",
                            )

            final_text, result, state_metadata = self._final_result(
                final_state, "".join(deltas), metadata
            )
            metadata.update(state_metadata)
            joined = "".join(deltas)
            if final_text and final_text != joined:
                missing = final_text[len(joined) :] if final_text.startswith(joined) else final_text
                if missing:
                    deltas.append(missing)
                    await self._publish_delta(handle, message_id, missing)
            await asyncio.shield(
                self.store.finish_run(
                    thread_id=handle.thread_id,
                    run_id=handle.run_id,
                    status="succeeded",
                    final_text=final_text,
                    result=result,
                    message_id=message_id,
                    is_partial=False,
                )
            )
            handle.status = "succeeded"
            await self.broker.publish(
                EventType.TEXT_MESSAGE_END,
                handle.thread_id,
                handle.run_id,
                data={"message_id": message_id},
            )
            await self.broker.publish(
                EventType.CUSTOM,
                handle.thread_id,
                handle.run_id,
                message="任务结果已生成",
                data={"name": "task_result", "result": result},
            )
            await self.broker.publish(
                EventType.RUN_FINISHED,
                handle.thread_id,
                handle.run_id,
                data={
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "metadata": metadata,
                },
            )
        except asyncio.CancelledError:
            partial = "".join(deltas)
            await asyncio.shield(
                self.store.finish_run(
                    thread_id=handle.thread_id,
                    run_id=handle.run_id,
                    status="cancelled",
                    final_text=partial,
                    result=None,
                    message_id=message_id,
                    is_partial=bool(partial),
                )
            )
            if message_started:
                await self.broker.publish(
                    EventType.TEXT_MESSAGE_END,
                    handle.thread_id,
                    handle.run_id,
                    data={"message_id": message_id, "partial": bool(partial)},
                )
            await self.broker.publish(
                EventType.TASK_CANCELLED,
                handle.thread_id,
                handle.run_id,
                message="任务已取消",
                data={"cancelled_at": utc_now(), "reason": "user_requested"},
            )
            handle.status = "cancelled"
        except Exception:
            logger.exception(
                "Agent run failed for thread_id=%s run_id=%s",
                handle.thread_id,
                handle.run_id,
            )
            partial = "".join(deltas)
            public_message = "任务执行失败，请稍后重试"
            await asyncio.shield(
                self.store.finish_run(
                    thread_id=handle.thread_id,
                    run_id=handle.run_id,
                    status="failed",
                    final_text=partial,
                    result=None,
                    message_id=message_id,
                    is_partial=bool(partial),
                    error_code="AGENT_RUN_FAILED",
                    error_message=public_message,
                )
            )
            if message_started:
                await self.broker.publish(
                    EventType.TEXT_MESSAGE_END,
                    handle.thread_id,
                    handle.run_id,
                    data={"message_id": message_id, "partial": bool(partial)},
                )
            await self.broker.publish(
                EventType.RUN_ERROR,
                handle.thread_id,
                handle.run_id,
                message=public_message,
                data={
                    "code": "AGENT_RUN_FAILED",
                    "message": public_message,
                    "retryable": True,
                },
            )
            handle.status = "failed"
        finally:
            if self.active_tasks.get(handle.thread_id) is handle:
                self.active_tasks.pop(handle.thread_id, None)
            handle.done_event.set()

    async def _publish_delta(
        self,
        handle: RunHandle,
        message_id: str,
        delta: str,
        *,
        model_role: str | None = None,
    ) -> None:
        data: dict[str, Any] = {"message_id": message_id, "delta": delta}
        if model_role:
            data["model_role"] = model_role
        await self.broker.publish(
            EventType.TEXT_MESSAGE_CONTENT,
            handle.thread_id,
            handle.run_id,
            data=data,
        )

    def _final_result(
        self,
        state: dict[str, Any] | None,
        streamed_text: str,
        metadata: dict[str, Any],
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        terminal = state.get("terminal_result") if state else None
        terminal = terminal if isinstance(terminal, dict) else {}
        final_text = str(
            terminal.get("final_text") or terminal.get("message") or streamed_text or ""
        )
        if not final_text and state:
            messages = state.get("messages") or []
            if messages:
                value = getattr(messages[-1], "content", "")
                if isinstance(value, str):
                    final_text = value
        raw_status = terminal.get("status")
        status = (
            raw_status
            if raw_status in {"complete", "incomplete", "not_configured", "error"}
            else "complete"
        )
        memory_status = metadata.get("memory_status", "not_configured")
        picks = terminal.get("picks", [])
        if picks:
            final_text = sanitize_shopping_markdown(final_text)
        result = {
            "status": status,
            "final_text": final_text,
            "picks": picks,
            "unresolved": (
                visible_unresolved(terminal.get("unresolved", []))
                if picks
                else terminal.get("unresolved", [])
            ),
            "learned_preferences": _merged_preferences(
                state.get("learned_preferences", []) if state else [],
                metadata.get("learned_preferences", []),
                terminal.get("learned_preferences", []),
            ),
            "memory_status": memory_status,
            "source_kind": "offline_snapshot",
            "artifacts": [],
        }
        result = enrich_task_result(
            result, self.product_image_catalog_path
        ) or result
        state_metadata = {
            "phase": state.get("phase") if state else metadata.get("phase"),
            "iteration": state.get("iteration", 0) if state else metadata.get("iteration", 0),
        }
        return final_text, result, state_metadata

    async def close(self) -> None:
        self._closing = True
        handles = list(self.active_tasks.values())
        for handle in handles:
            if handle.task is not None and not handle.task.done():
                handle.status = "cancelling"
                handle.task.cancel()
        if handles:
            await asyncio.gather(
                *(handle.task for handle in handles if handle.task is not None),
                return_exceptions=True,
            )
