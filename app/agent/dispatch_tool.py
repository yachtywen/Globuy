"""Tool registry, dispatch meta-tool and post-model routing."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal

from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, MessagesState
from langgraph.prebuilt import ToolNode

from app.agent.middleware import guarded_tool_call
from app.tools import build_core_tools
from app.utils.thread_ctx import current_fork_depth, current_thread_id

if TYPE_CHECKING:
    from app.agent.main_agent import AgentLoop


def get_core_tools(
    names: Iterable[str] | None = None,
    *,
    model=None,
) -> list[BaseTool]:
    tools = list(build_core_tools(model))
    if names is None:
        return tools
    selected = set(names)
    unknown = selected - {tool.name for tool in tools}
    if unknown:
        raise KeyError(f"未知工具: {', '.join(sorted(unknown))}")
    return [tool for tool in tools if tool.name in selected]


def build_dispatch_node(tools: Iterable[BaseTool]) -> ToolNode:
    return ToolNode(
        list(tools),
        handle_tool_errors=True,
        awrap_tool_call=guarded_tool_call,
    )


def build_dispatch_tool(owner: AgentLoop) -> BaseTool:
    @tool("dispatch_tool")
    async def dispatch_tool(demand: str) -> dict:
        """Run one independent demand in a homogeneous child Agent."""

        return await owner.dispatch(demand.strip())

    return dispatch_tool


def fork_dispatch_allowed(max_depth: int) -> bool:
    return current_thread_id() is not None and current_fork_depth() < max_depth


def route_after_assistant(state: MessagesState) -> Literal["tools", "__end__"]:
    last_message = state["messages"][-1]
    if getattr(last_message, "tool_calls", None):
        return "tools"
    return END
