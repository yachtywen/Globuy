"""Automatic memory extraction/action tests with deterministic fakes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.memory.manager import ExtractedMemory, MemoryManager, ProposedAction


class FakeStore:
    def __init__(self, matches: list[tuple[str, str]] | None = None) -> None:
        self.matches = matches or []
        self.queries: list[str] = []

    async def asearch_for_consolidation(self, user_id: str, *, query: str, limit: int):
        assert user_id == "user-1"
        self.queries.append(query)
        return [
            SimpleNamespace(key=memory_id, value={"memory": memory})
            for memory_id, memory in self.matches[:limit]
        ]


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict], dict]] = []

    async def apply_actions(self, user_id: str, actions: list[dict], **kwargs):
        self.calls.append((user_id, actions, kwargs))
        return [
            {
                "id": action.get("memory_id") or "new-memory",
                "event": action["event"],
                "summary": action["memory"],
            }
            for action in actions
            if action["event"] != "NONE"
        ]


class FakeMemoryManager(MemoryManager):
    def __init__(
        self, facts: list[ExtractedMemory], actions: list[ProposedAction], store, service
    ):
        super().__init__(model=object(), store=store, service=service)
        self.facts = facts
        self.actions = actions

    async def _extract(self, context_messages, messages, cache_key, config):
        del context_messages, messages, cache_key, config
        return self.facts

    async def _decide(self, facts, existing, cache_key, config):
        return self.actions


async def process(manager: MemoryManager):
    return await manager.process(
        user_id="user-1",
        thread_id="thread-1",
        run_id="run-1",
        messages=[
            {"role": "user", "content": "我长期偏好黑色耳机"},
            {"role": "assistant", "content": "知道了"},
        ],
    )


@pytest.mark.asyncio
async def test_add_action_is_automatically_written() -> None:
    service = FakeService()
    manager = FakeMemoryManager(
        [ExtractedMemory(fact_index=0, memory="用户长期偏好黑色耳机", keywords=["耳麦"])],
        [ProposedAction(event="ADD", fact_index=0, memory="用户长期偏好黑色耳机")],
        FakeStore(),
        service,
    )
    changes = await process(manager)
    assert changes[0]["event"] == "ADD"
    assert service.calls[0][1][0]["memory_id"] is None
    assert service.calls[0][1][0]["keywords"] == ["耳麦"]


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["UPDATE", "DELETE", "NONE"])
async def test_existing_integer_ids_are_mapped_to_real_ids(event: str) -> None:
    service = FakeService()
    memory = "用户现在偏好白色耳机" if event == "UPDATE" else ""
    manager = FakeMemoryManager(
        [ExtractedMemory(fact_index=0, memory="用户不再偏好黑色耳机")],
        [ProposedAction(event=event, fact_index=0, id=0, memory=memory)],
        FakeStore([("real-uuid", "用户偏好黑色耳机")]),
        service,
    )
    await process(manager)
    assert service.calls[0][1][0]["memory_id"] == "real-uuid"


@pytest.mark.asyncio
async def test_forged_integer_id_causes_zero_writes() -> None:
    service = FakeService()
    manager = FakeMemoryManager(
        [ExtractedMemory(fact_index=0, memory="用户偏好白色耳机")],
        [ProposedAction(event="UPDATE", fact_index=0, id=7, memory="用户偏好白色耳机")],
        FakeStore([("real-uuid", "用户偏好黑色耳机")]),
        service,
    )
    with pytest.raises(ValueError, match="outside the retrieved set"):
        await process(manager)
    assert service.calls == []


@pytest.mark.asyncio
async def test_temporary_and_sensitive_facts_are_filtered_before_decision() -> None:
    service = FakeService()
    store = FakeStore()
    manager = FakeMemoryManager(
        [
            ExtractedMemory(fact_index=0, memory="这次预算是 500 元"),
            ExtractedMemory(fact_index=1, memory="api_key=secret-value"),
        ],
        [ProposedAction(event="ADD", fact_index=0, memory="不应执行")],
        store,
        service,
    )
    assert await process(manager) == []
    assert store.queries == []
    assert service.calls == []


@pytest.mark.asyncio
async def test_duplicate_fact_index_causes_zero_writes() -> None:
    service = FakeService()
    manager = FakeMemoryManager(
        [
            ExtractedMemory(fact_index=0, memory="偏好静音键盘"),
            ExtractedMemory(fact_index=0, memory="偏好无线键盘"),
        ],
        [],
        FakeStore(),
        service,
    )
    with pytest.raises(ValueError, match="duplicate fact_index"):
        await process(manager)
    assert service.calls == []


@pytest.mark.asyncio
async def test_every_fact_requires_exactly_one_action() -> None:
    service = FakeService()
    manager = FakeMemoryManager(
        [
            ExtractedMemory(fact_index=0, memory="偏好静音键盘"),
            ExtractedMemory(fact_index=1, memory="偏好无线键盘"),
        ],
        [ProposedAction(event="ADD", fact_index=0, memory="偏好静音键盘")],
        FakeStore(),
        service,
    )
    with pytest.raises(ValueError, match="exactly one action"):
        await process(manager)
    assert service.calls == []
