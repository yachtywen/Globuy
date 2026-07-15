"""Small AG-UI-inspired event envelope used by the WebSocket transport."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    RUN_STARTED = "RUN_STARTED"
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_END = "TOOL_CALL_END"
    TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
    TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
    TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
    RUN_FINISHED = "RUN_FINISHED"
    RUN_ERROR = "RUN_ERROR"
    TASK_CANCELLED = "TASK_CANCELLED"


class AgentEvent(BaseModel):
    type: EventType
    thread_id: str
    run_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = Field(default_factory=dict)


def event(event_type: EventType, thread_id: str, run_id: str, **data: Any) -> AgentEvent:
    return AgentEvent(type=event_type, thread_id=thread_id, run_id=run_id, data=data)
