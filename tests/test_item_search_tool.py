import importlib
import json
from pathlib import Path

import pytest
from langchain_core.callbacks.base import AsyncCallbackHandler
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from app.agent.dispatch_tool import build_dispatch_node
from app.api.monitor import AgentEvent, EventType, Monitor, monitor_scope
from app.config import get_settings
from app.search.schemas import Candidate
from app.tools import item_search as exported_item_search
from app.utils.thread_ctx import thread_scope

item_search_module = importlib.import_module("app.tools.item_search")


class ToolLifecycleRecorder(AsyncCallbackHandler):
    def __init__(self) -> None:
        self.starts: list[dict] = []
        self.ends: list[object] = []

    async def on_tool_start(self, _serialized, _input_str, **kwargs) -> None:
        self.starts.append(kwargs)

    async def on_tool_end(self, output, **_kwargs) -> None:
        self.ends.append(output)


@pytest.mark.asyncio
async def test_middleware_rejection_emits_exactly_one_tool_lifecycle() -> None:
    recorder = ToolLifecycleRecorder()
    call = {
        "name": "item_search",
        "type": "tool_call",
        "id": "rejected-call-1",
        "args": {"query": "耳机", "platform": "jingdong"},
    }
    builder = StateGraph(MessagesState)
    builder.add_node("tools", build_dispatch_node([exported_item_search]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)

    state = await builder.compile().ainvoke(
        {"messages": [AIMessage(content="", tool_calls=[call])]},
        config={"callbacks": [recorder]},
    )

    assert json.loads(state["messages"][-1].content)["status"] == "needs_planning"
    assert len(recorder.starts) == 1
    assert len(recorder.ends) == 1
    assert recorder.starts[0]["tool_call_id"] == "rejected-call-1"


class FakeCoordinator:
    def __init__(self) -> None:
        self.intents = []
        self.repository = FakeDirectRepository(
            [
                Candidate(
                    item_id="jingdong:1",
                    platform="jingdong",
                    title="主动降噪蓝牙耳机",
                    price=299,
                    currency="CNY",
                    product_url="https://example.test/item/1",
                    retrieval_rank=1,
                )
            ]
        )

    async def ensure(self, intent, *, target_total=None):
        self.intents.append(intent)
        return None


class FakeDirectRepository:
    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates
        self.calls = []

    async def load_scope_candidates(self, scope, *, filters, limit):
        self.calls.append((scope, filters, limit))
        return self.candidates[:limit]


class FakeDirectCoordinator:
    def __init__(self, candidates: list[Candidate]) -> None:
        self.repository = FakeDirectRepository(candidates)

    async def ensure(self, intent, *, target_total=None):
        raise AssertionError("disabled provider must not be called")


@pytest.mark.asyncio
async def test_item_search_tool_returns_contract_and_monitor_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[tuple[str, AgentEvent]] = []

    async def publish(thread_id: str, item: AgentEvent) -> None:
        events.append((thread_id, item))

    coordinator = FakeCoordinator()
    monkeypatch.setattr(item_search_module, "get_catalog_runtime", lambda: coordinator)
    settings = get_settings().model_copy(update={"product_provider": "none"})
    monkeypatch.setattr(item_search_module, "get_settings", lambda: settings)
    with (
        thread_scope("root", tmp_path, run_id="run-1"),
        monitor_scope(Monitor(publish, publish_thread_id="root")),
    ):
        node = build_dispatch_node([exported_item_search])
        builder = StateGraph(MessagesState)
        builder.add_node("tools", node)
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        graph = builder.compile()
        state = await graph.ainvoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "item_search",
                                "type": "tool_call",
                                "id": "call-1",
                                "args": {
                                    "query": "降噪耳机",
                                    "platform": "jingdong",
                                    "top_k": 1,
                                    "intent": {
                                        "category_key": "headphones",
                                        "category_name": "耳机",
                                        "primary_query": "降噪耳机",
                                        "platforms": ["jingdong"],
                                    },
                                },
                            }
                        ],
                    )
                ],
            }
        )

    payload = json.loads(state["messages"][-1].content)
    assert payload["status"] == "ok"
    assert payload["candidates"][0]["retrieval_rank"] == 1
    assert [item.type for _, item in events if item.type != EventType.CUSTOM] == [
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_RESULT,
        EventType.TOOL_CALL_END,
    ]
    assert any(item.type == EventType.CUSTOM for _, item in events)
    assert all(channel == "root" for channel, _ in events)
    tool_end = next(item for _, item in events if item.type == EventType.TOOL_CALL_END)
    tool_result = next(item for _, item in events if item.type == EventType.TOOL_CALL_RESULT)
    assert tool_end.data["duration_ms"] >= 0
    assert tool_result.data["result"]["tool_result_estimated_tokens"] > 0


@pytest.mark.asyncio
async def test_item_search_hydrates_only_its_requested_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = FakeCoordinator()
    monkeypatch.setattr(
        item_search_module,
        "get_catalog_runtime",
        lambda: coordinator,
    )
    settings = get_settings().model_copy(update={"product_provider": "justone"})
    monkeypatch.setattr(item_search_module, "get_settings", lambda: settings)

    payload = await exported_item_search.ainvoke(
        {
            "query": "牛仔裤",
            "platform": "taobao",
            "intent": {
                "category_key": "jeans",
                "category_name": "牛仔裤",
                "primary_query": "牛仔裤",
                "platforms": ["taobao", "jingdong", "douyin"],
            },
        }
    )

    assert payload["status"] == "ok"
    assert len(coordinator.intents) == 1
    assert coordinator.intents[0].platforms == ["taobao"]


@pytest.mark.asyncio
async def test_faiss_path_reads_fresh_postgres_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = FakeDirectCoordinator(
        [
            Candidate(
                item_id="taobao:1",
                platform="taobao",
                title="降噪耳机",
                price=299,
                currency="CNY",
                product_url="https://example.test/taobao/1",
                source_rank=1,
            )
        ]
    )
    settings = get_settings().model_copy(update={"product_provider": "none"})
    monkeypatch.setattr(item_search_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        item_search_module,
        "get_catalog_runtime",
        lambda: coordinator,
    )

    payload = await exported_item_search.ainvoke(
        {
            "query": "降噪耳机",
            "platform": "taobao",
            "top_k": 50,
            "intent": {
                "category_key": "headphones",
                "category_name": "耳机",
                "primary_query": "降噪耳机",
                "platforms": ["taobao"],
            },
        }
    )

    assert payload["status"] == "ok"
    assert payload["search_strategy"] == "faiss"
    assert payload["retrieval_route"] == "category_faiss"
    assert payload["candidates"][0]["source_rank"] == 1
    assert coordinator.repository.calls[0][2] == settings.faiss_candidates_per_platform


@pytest.mark.asyncio
async def test_chat_fallback_guard_replaces_unverified_search_claims() -> None:
    """Middleware must rewrite chat_fallback messages that claim the search
    chain is unavailable (the exact hallucination seen in live runs)."""
    from app.tools.chat_fallback import chat_fallback

    for bad_message in (
        "抱歉，我目前无法直接搜索商品数据库。不过我可以为您推荐：",
        "我的检索工具暂时无法使用。",
        "当前系统服务不可用，请稍后再试。",
    ):
        call = {
            "name": "chat_fallback",
            "type": "tool_call",
            "id": f"claim-{hash(bad_message)}",
            "args": {"message": bad_message},
        }
        builder = StateGraph(MessagesState)
        builder.add_node("tools", build_dispatch_node([chat_fallback]))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        state = await builder.compile().ainvoke(
            {"messages": [AIMessage(content="", tool_calls=[call])]}
        )
        payload = json.loads(state["messages"][-1].content)
        assert payload["message"] == (
            "当前没有检索到可核验的商品结果，请补充具体品牌或型号，或稍后再试。"
        )
