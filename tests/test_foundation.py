from pathlib import Path

import pytest

from app.agent.llm import get_chat_model
from app.agent.prompts import get_planner_prompt, get_shopping_summary_prompt, get_system_prompt
from app.api.monitor import AgentEvent, EventType, Monitor
from app.recall import FaissHNSWIndex
from app.utils.path_utils import safe_join, upload_path
from app.utils.thread_ctx import (
    current_fork_depth,
    current_run_id,
    current_session_dir,
    current_thread_id,
    fork_scope,
    thread_scope,
)


def test_task_context_is_scoped_and_fork_inherits_session(tmp_path: Path) -> None:
    session = tmp_path / "session"
    with thread_scope("parent", session, run_id="run-1", user_id="user-1"):
        assert current_thread_id() == "parent"
        assert current_run_id() == "run-1"
        with fork_scope("parent-fork-1"):
            assert current_thread_id() == "parent-fork-1"
            assert current_session_dir() == session
            assert current_fork_depth() == 1
        assert current_thread_id() == "parent"
    assert current_thread_id() is None


@pytest.mark.asyncio
async def test_monitor_routes_context_events(tmp_path: Path) -> None:
    published: list[tuple[str, AgentEvent]] = []

    async def publish(thread_id: str, item: AgentEvent) -> None:
        published.append((thread_id, item))

    monitor = Monitor(publish)
    with thread_scope("thread-1", tmp_path, run_id="run-1"):
        await monitor.report_tool_start("call-1", "planner", {"goal": "耳机"})
        await monitor.report_fork("thread-1-fork-1", reason="并行检索", tool_names=["item_search"])

    assert [item.type for _, item in published] == [
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.CUSTOM,
    ]
    assert all(thread_id == "thread-1" for thread_id, _ in published)


def test_paths_are_sanitized_below_roots(tmp_path: Path) -> None:
    uploaded = upload_path(tmp_path / "uploaded", "../thread one")
    assert uploaded.parent == (tmp_path / "uploaded").resolve()
    assert uploaded.name == "thread-one"
    joined = safe_join(tmp_path / "output", "../../outside", "item.json")
    assert (tmp_path / "output").resolve() in joined.parents


def test_prompt_contract_contains_fork_and_summary_rules() -> None:
    system = get_system_prompt("budget: 1000 CNY")
    assert "工具调用链不少于 3 步" in system
    assert "budget: 1000 CNY" in system
    assert "hard_constraints" in get_planner_prompt()
    assert "最多推荐 3 项" in get_shopping_summary_prompt()


def test_mock_model_is_process_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLOBUY_MODEL_PROVIDER", "mock")
    get_chat_model.cache_clear()
    try:
        assert get_chat_model() is get_chat_model()
    finally:
        get_chat_model.cache_clear()


def test_faiss_hnsw_round_trip(tmp_path: Path) -> None:
    index = FaissHNSWIndex(3, hnsw_m=8)
    index.add([101, 202], [[1, 0, 0], [0, 1, 0]])
    assert index.search([0.9, 0.1, 0], limit=1)[0][0] == 101

    path = tmp_path / "items.faiss"
    index.save(path)
    loaded = FaissHNSWIndex.load(path)
    assert loaded.search([0, 1, 0], limit=1)[0][0] == 202
