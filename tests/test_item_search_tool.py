import importlib
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from app.agent.dispatch_tool import build_dispatch_node
from app.api.monitor import AgentEvent, EventType, Monitor, monitor_scope
from app.search.schemas import Candidate, ItemSearchOutput
from app.tools import item_search as exported_item_search
from app.utils.thread_ctx import thread_scope

item_search_module = importlib.import_module("app.tools.item_search")


class FakeSearchService:
    def search(self, query, platform, top_k, filters):
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
            truncated=True,
        )


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
    with thread_scope("root", tmp_path, run_id="run-1"), monitor_scope(
        Monitor(publish, publish_thread_id="root")
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
    assert [item.type for _, item in events] == [
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_RESULT,
        EventType.TOOL_CALL_END,
    ]
    assert all(channel == "root" for channel, _ in events)
