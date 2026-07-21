"""Per-run event sequencing, replay buffers, and independent subscribers."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.api.monitor import AgentEvent, EventType


def _now() -> datetime:
    return datetime.now(UTC)


class MonitorEvent(BaseModel):
    type: Literal["monitor_event"] = "monitor_event"
    schema_version: Literal["1.0"] = "1.0"
    event: EventType
    event_id: str
    sequence: int | None
    thread_id: str
    run_id: str
    timestamp: datetime = Field(default_factory=_now)
    message: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


@dataclass(eq=False)
class Subscription:
    thread_id: str
    run_id: str
    queue: asyncio.Queue[MonitorEvent | None]
    subscription_id: str = field(default_factory=lambda: uuid4().hex)
    overflowed: bool = False


@dataclass
class RunEventStream:
    thread_id: str
    run_id: str
    max_events: int
    next_sequence: int = 1
    buffer: deque[MonitorEvent] = field(default_factory=deque)
    subscribers: set[Subscription] = field(default_factory=set)
    terminal_event: MonitorEvent | None = None
    terminal_at: datetime | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class EventBroker:
    def __init__(
        self,
        *,
        buffer_size: int = 2_000,
        retention_seconds: int = 1_800,
        subscriber_queue_size: int = 256,
    ) -> None:
        self.buffer_size = buffer_size
        self.retention = timedelta(seconds=retention_seconds)
        self.subscriber_queue_size = subscriber_queue_size
        self._streams: dict[tuple[str, str], RunEventStream] = {}
        self._lock = asyncio.Lock()

    async def ensure_stream(self, thread_id: str, run_id: str) -> RunEventStream:
        key = (thread_id, run_id)
        async with self._lock:
            self._prune_locked()
            stream = self._streams.get(key)
            if stream is None:
                stream = RunEventStream(thread_id, run_id, self.buffer_size)
                self._streams[key] = stream
            return stream

    def _prune_locked(self) -> None:
        cutoff = _now() - self.retention
        expired = [
            key
            for key, stream in self._streams.items()
            if stream.terminal_at is not None
            and stream.terminal_at < cutoff
            and not stream.subscribers
        ]
        for key in expired:
            self._streams.pop(key, None)

    async def publish(
        self,
        event_type: EventType,
        thread_id: str,
        run_id: str,
        *,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> MonitorEvent:
        stream = await self.ensure_stream(thread_id, run_id)
        async with stream.lock:
            if event_type in {
                EventType.RUN_FINISHED,
                EventType.RUN_ERROR,
                EventType.TASK_CANCELLED,
            } and stream.terminal_event is not None:
                return stream.terminal_event
            sequence = stream.next_sequence
            stream.next_sequence += 1
            item = MonitorEvent(
                event=event_type,
                event_id=f"{run_id}:{sequence}",
                sequence=sequence,
                thread_id=thread_id,
                run_id=run_id,
                message=message,
                data=data or {},
            )
            stream.buffer.append(item)
            while len(stream.buffer) > stream.max_events:
                stream.buffer.popleft()
            if event_type in {
                EventType.RUN_FINISHED,
                EventType.RUN_ERROR,
                EventType.TASK_CANCELLED,
            }:
                if stream.terminal_event is None:
                    stream.terminal_event = item
                    stream.terminal_at = _now()
            for subscriber in list(stream.subscribers):
                try:
                    subscriber.queue.put_nowait(item)
                except asyncio.QueueFull:
                    subscriber.overflowed = True
                    stream.subscribers.discard(subscriber)
                    while not subscriber.queue.empty():
                        subscriber.queue.get_nowait()
                    subscriber.queue.put_nowait(None)
            return item

    async def publish_internal(self, channel_thread_id: str, item: AgentEvent) -> None:
        """Convert context-local monitor output to the root run's public envelope."""

        data = dict(item.data)
        message = data.pop("message", None)
        if item.thread_id != channel_thread_id:
            data.setdefault("source_thread_id", item.thread_id)
        await self.publish(
            item.type,
            channel_thread_id,
            item.run_id,
            message=message if isinstance(message, str) else None,
            data=data,
        )

    @staticmethod
    def control_event(
        name: str,
        thread_id: str,
        run_id: str,
        *,
        message: str | None = None,
        **data: Any,
    ) -> MonitorEvent:
        return MonitorEvent(
            event=EventType.CUSTOM,
            event_id=f"connection_{uuid4().hex}:{name}",
            sequence=None,
            thread_id=thread_id,
            run_id=run_id,
            message=message,
            data={"name": name, **data},
        )

    async def subscribe(
        self, thread_id: str, run_id: str, after: int
    ) -> Subscription:
        stream = await self.ensure_stream(thread_id, run_id)
        async with stream.lock:
            replay = [item for item in stream.buffer if (item.sequence or 0) > after]
            queue = asyncio.Queue[MonitorEvent | None](
                maxsize=max(self.subscriber_queue_size, len(replay) + 3)
            )
            subscriber = Subscription(thread_id, run_id, queue)
            earliest = stream.buffer[0].sequence if stream.buffer else stream.next_sequence
            latest = stream.next_sequence - 1
            if earliest is not None and after < earliest - 1:
                queue.put_nowait(
                    self.control_event(
                        "replay_gap",
                        thread_id,
                        run_id,
                        message="部分历史事件已不可用",
                        requested_after=after,
                        earliest_available_sequence=earliest,
                    )
                )
            for item in replay:
                queue.put_nowait(item)
            queue.put_nowait(
                self.control_event(
                    "stream_ready",
                    thread_id,
                    run_id,
                    message="事件流已连接",
                    replayed_through=latest,
                    earliest_available_sequence=earliest,
                )
            )
            stream.subscribers.add(subscriber)
            return subscriber

    async def unsubscribe(self, subscriber: Subscription) -> None:
        key = (subscriber.thread_id, subscriber.run_id)
        async with self._lock:
            stream = self._streams.get(key)
        if stream is not None:
            async with stream.lock:
                stream.subscribers.discard(subscriber)

    async def cursor_info(self, thread_id: str, run_id: str) -> dict[str, Any]:
        async with self._lock:
            stream = self._streams.get((thread_id, run_id))
        if stream is None:
            return {
                "last_sequence": 0,
                "earliest_available_sequence": None,
                "terminal_event": None,
            }
        async with stream.lock:
            return {
                "last_sequence": stream.next_sequence - 1,
                "earliest_available_sequence": (
                    stream.buffer[0].sequence if stream.buffer else None
                ),
                "terminal_event": (
                    stream.terminal_event.model_dump(mode="json")
                    if stream.terminal_event
                    else None
                ),
            }

    async def close(self) -> None:
        async with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for stream in streams:
            async with stream.lock:
                subscribers = list(stream.subscribers)
                stream.subscribers.clear()
            for subscriber in subscribers:
                while not subscriber.queue.empty():
                    subscriber.queue.get_nowait()
                subscriber.queue.put_nowait(None)
