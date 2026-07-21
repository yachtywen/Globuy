"""AG-UI event envelopes and context-aware task monitoring."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.utils.thread_ctx import current_run_id, current_thread_id


class EventType(StrEnum):
    RUN_STARTED = "RUN_STARTED"
    STEP_STARTED = "STEP_STARTED"
    STEP_FINISHED = "STEP_FINISHED"
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
    TOOL_CALL_END = "TOOL_CALL_END"
    TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
    TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
    TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
    TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
    RUN_FINISHED = "RUN_FINISHED"
    RUN_ERROR = "RUN_ERROR"
    TASK_CANCELLED = "TASK_CANCELLED"
    CUSTOM = "CUSTOM"


class AgentEvent(BaseModel):
    type: EventType
    thread_id: str
    run_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = Field(default_factory=dict)


def event(event_type: EventType, thread_id: str, run_id: str, **data: Any) -> AgentEvent:
    return AgentEvent(type=event_type, thread_id=thread_id, run_id=run_id, data=data)


EventPublisher = Callable[[str, AgentEvent], Awaitable[None]]
monitor_var: ContextVar["Monitor | None"] = ContextVar("monitor", default=None)


class Monitor:
    """Publish task events without coupling tools to WebSocket objects."""

    def __init__(
        self, publisher: EventPublisher, *, publish_thread_id: str | None = None
    ) -> None:
        self._publisher = publisher
        self._publish_thread_id = publish_thread_id

    async def emit(self, event_type: EventType, **data: Any) -> AgentEvent:
        thread_id = current_thread_id()
        run_id = current_run_id()
        if thread_id is None or run_id is None:
            raise RuntimeError("Monitor.emit 必须在包含 thread_id/run_id 的 thread_scope 内调用")
        agent_event = event(event_type, thread_id, run_id, **data)
        await self._publisher(self._publish_thread_id or thread_id, agent_event)
        return agent_event

    async def report_tool_start(
        self, tool_call_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> None:
        await self.emit(
            EventType.TOOL_CALL_START,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        await self.emit(
            EventType.TOOL_CALL_ARGS,
            tool_call_id=tool_call_id,
            arguments=arguments,
        )

    async def report_tool_end(self, tool_call_id: str, result: Any) -> None:
        await self.emit(
            EventType.TOOL_CALL_RESULT,
            tool_call_id=tool_call_id,
            result=result,
        )
        end_data: dict[str, Any] = {"tool_call_id": tool_call_id}
        if isinstance(result, dict):
            end_data["status"] = result.get("status", "ok")
            if "duration_ms" in result:
                end_data["duration_ms"] = result["duration_ms"]
        await self.emit(EventType.TOOL_CALL_END, **end_data)

    async def report_fork(
        self, child_thread_id: str, *, reason: str, tool_names: list[str]
    ) -> None:
        await self.emit(
            EventType.CUSTOM,
            name="agent_fork",
            child_thread_id=child_thread_id,
            reason=reason,
            tool_names=tool_names,
        )

    async def report_task_result(self, result: Any) -> None:
        await self.emit(EventType.CUSTOM, name="task_result", result=result)

    async def report_error(self, message: str) -> None:
        await self.emit(EventType.RUN_ERROR, message=message)


@contextmanager
def monitor_scope(monitor: Monitor) -> Iterator[None]:
    token = monitor_var.set(monitor)
    try:
        yield
    finally:
        monitor_var.reset(token)


def current_monitor() -> Monitor | None:
    return monitor_var.get()
