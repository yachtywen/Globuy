import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from app.agent.dispatch_tool import build_dispatch_node
from app.agent.main_agent import AgentLoop
from app.api.monitor import AgentEvent, EventType, Monitor, monitor_scope
from app.tools.planner import planner
from app.utils.thread_ctx import fork_scope, thread_scope


def dispatch_tool(loop: AgentLoop):
    return next(tool for tool in loop.tools if tool.name == "dispatch_tool")


class ForkCompletionModel:
    def __init__(self) -> None:
        self.responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "planner",
                        "args": {"goal": "耳机"},
                        "id": "probe-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="子任务检索完成。"),
        ]

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages, config=None):
        del config
        return self.responses.pop(0)

@pytest.mark.asyncio
async def test_dispatch_creates_homogeneous_child_and_reports_fork(tmp_path: Path) -> None:
    published: list[tuple[str, AgentEvent]] = []

    async def publish(channel: str, item: AgentEvent) -> None:
        published.append((channel, item))

    parent = AgentLoop(model=None)
    with thread_scope("root", tmp_path, run_id="run-1"), monitor_scope(
        Monitor(publish, publish_thread_id="root")
    ):
        message = await dispatch_tool(parent).ainvoke(
            {
                "name": "dispatch_tool",
                "type": "tool_call",
                "id": "dispatch-1",
                "args": {"demand": "在京东搜索降噪耳机"},
            }
        )

    payload = json.loads(message.content)
    child = parent.children[payload["child_thread_id"]]
    assert payload["status"] == "ok"
    assert "在京东搜索降噪耳机" in payload["answer"]
    assert [tool.name for tool in child.tools] == [tool.name for tool in parent.tools]
    assert child.system_prompt == parent.system_prompt
    assert child.model is parent.model
    assert any(item.data.get("name") == "agent_fork" for _, item in published)
    assert all(channel == "root" for channel, _ in published)
    assert EventType.CUSTOM in [item.type for _, item in published]
    assert EventType.STEP_STARTED in [item.type for _, item in published]
    assert parent.active_children == {}


@pytest.mark.asyncio
async def test_real_model_style_fork_can_return_without_parent_terminal_tool(
    tmp_path: Path,
) -> None:
    parent = AgentLoop(
        model=ForkCompletionModel(),
        tools=[planner],
        enable_dispatch=True,
    )

    with thread_scope("root", tmp_path, run_id="run-fork-completion"):
        payload = await parent.dispatch("检索一款耳机")

    assert payload["status"] == "ok"
    assert payload["answer"] == "子任务检索完成。"
    assert parent.active_children == {}


@pytest.mark.asyncio
async def test_nested_dispatch_is_rejected(tmp_path: Path) -> None:
    parent = AgentLoop(model=None)
    with thread_scope("root", tmp_path, run_id="run-1"), fork_scope("root-fork-1"):
        message = await dispatch_tool(parent).ainvoke(
            {
                "name": "dispatch_tool",
                "type": "tool_call",
                "id": "dispatch-2",
                "args": {"demand": "继续派生"},
            }
        )

    payload = json.loads(message.content)
    assert payload["status"] == "depth_limit"
    assert payload["search_results"] == []


@pytest.mark.asyncio
async def test_tool_node_executes_three_dispatch_calls_concurrently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent = AgentLoop(model=None)
    active = 0
    maximum_active = 0
    all_started = asyncio.Event()

    async def fake_dispatch(demand: str) -> dict:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        active -= 1
        return {"status": "ok", "answer": demand, "search_results": []}

    monkeypatch.setattr(parent, "dispatch", fake_dispatch)
    calls = [
        {
            "name": "dispatch_tool",
            "args": {"demand": f"搜索平台 {index}"},
            "id": f"call-{index}",
            "type": "tool_call",
        }
        for index in range(3)
    ]
    builder = StateGraph(MessagesState)
    builder.add_node("tools", build_dispatch_node([dispatch_tool(parent)]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()

    with thread_scope("root", tmp_path, run_id="run-1"):
        result = await graph.ainvoke({"messages": [AIMessage(content="", tool_calls=calls)]})

    assert maximum_active == 3
    assert len(result["messages"]) == 4
    assert sum(message.type == "tool" for message in result["messages"]) == 3
