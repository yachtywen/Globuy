"""Runs no longer synchronously process or expose long-term memory."""

from __future__ import annotations

import asyncio

import pytest

from app.api.event_broker import EventBroker
from app.api.run_registry import RunRegistry
from app.api.storage import SessionStore


async def _completed_run(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    await store.open()
    broker = EventBroker()

    async def agent(query: str, thread_id: str):
        del query, thread_id
        return "最终回答", {"status": "ok"}

    registry = RunRegistry(
        store=store,
        broker=broker,
        agent_runner=agent,
        stream_runner=None,
        session_dir=lambda thread_id: tmp_path / thread_id,
        product_image_catalog_path=tmp_path / "missing-products.jsonl",
    )
    thread = await registry.create_thread(
        user_id="user-1", current_thread_id=None, client_request_id="thread-request"
    )
    started = await registry.start_run(
        query="我长期偏好轻便背包",
        thread_id=thread["thread_id"],
        user_id="user-1",
        client_request_id="run-request",
    )
    for _ in range(100):
        record = await store.get_run(thread["thread_id"], started["run_id"])
        if record["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("run did not finish")
    subscription = await broker.subscribe(thread["thread_id"], started["run_id"], after=0)
    names: list[str] = []
    while not subscription.queue.empty():
        event = subscription.queue.get_nowait()
        if event is None:
            continue
        name = event.data.get("name")
        if isinstance(name, str):
            names.append(name)
    await broker.unsubscribe(subscription)
    status = await registry.run_status(thread["thread_id"], started["run_id"])
    await registry.close()
    await broker.close()
    await store.close()
    return status, names


@pytest.mark.asyncio
async def test_successful_run_has_no_public_memory_result_or_events(tmp_path) -> None:
    status, names = await _completed_run(tmp_path)
    assert status["status"] == "succeeded"
    assert "memory_status" not in status["result"]
    assert "memory_changes" not in status["result"]
    assert not any(name.startswith("memory_") for name in names)
