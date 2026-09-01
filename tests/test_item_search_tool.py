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
from app.search.schemas import Candidate, ItemSearchOutput
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


class FakeSearchService:
    def search(self, query, platform, top_k, filters, **kwargs):
        return ItemSearchOutput(
            status="ok",
            platform=platform,
            candidates=[
                Candidate(
                    item_id=f"{platform}:1",
                    platform=platform,
                    title="主动降噪蓝牙耳机",
                    price=299,
                    currency="CNY",
                    attributes={},
                    product_url="https://example.test/item/1",
                    retrieval_rank=1,
                )
            ],
            total_recall=3,
            catalog_candidate_count=3,
            truncated=True,
        )


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

    async def ensure(self, intent):
        self.intents.append(intent)
        return None


class FakeWorker:
    async def run_once(self, offer_ids):
        raise AssertionError("no newly hydrated offers expected")


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

    monkeypatch.setattr(
        item_search_module, "get_product_search_service", lambda: FakeSearchService()
    )
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
        item_search_module, "get_product_search_service", lambda: FakeSearchService()
    )
    monkeypatch.setattr(
        item_search_module,
        "get_catalog_runtime",
        lambda: (coordinator, FakeWorker()),
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
async def test_direct_search_reads_fresh_postgres_candidates_without_opensearch(
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
    settings = get_settings().model_copy(
        update={"product_provider": "none", "item_search_strategy": "direct_llm"}
    )
    monkeypatch.setattr(item_search_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        item_search_module,
        "get_catalog_runtime",
        lambda: (coordinator, FakeWorker()),
    )
    monkeypatch.setattr(
        item_search_module,
        "get_product_search_service",
        lambda: (_ for _ in ()).throw(AssertionError("OpenSearch must not be called")),
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
    assert payload["search_strategy"] == "direct_llm"
    assert payload["retrieval_route"] == "category_direct"
    assert payload["candidates"][0]["source_rank"] == 1
    assert coordinator.repository.calls[0][2] == settings.direct_candidates_per_platform


def test_progressive_strategy_honors_zero_and_full_rollout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(item_search_module, "current_user_id", lambda: "stable-user")
    base = get_settings().model_copy(update={"item_search_strategy": "progressive"})
    monkeypatch.setattr(
        item_search_module,
        "get_settings",
        lambda: base.model_copy(update={"direct_rerank_rollout_percent": 0}),
    )
    assert item_search_module.resolve_search_strategy() == "hybrid"
    monkeypatch.setattr(
        item_search_module,
        "get_settings",
        lambda: base.model_copy(update={"direct_rerank_rollout_percent": 100}),
    )
    assert item_search_module.resolve_search_strategy() == "intent_routed"
