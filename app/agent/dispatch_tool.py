"""Tool registry, dispatch node and post-model routing."""

from collections.abc import Iterable
from typing import Literal

from langchain_core.tools import BaseTool
from langgraph.graph import END, MessagesState
from langgraph.prebuilt import ToolNode

from app.tools import CORE_TOOLS


def get_core_tools(names: Iterable[str] | None = None) -> list[BaseTool]:
    tools = list(CORE_TOOLS)
    if names is None:
        return tools
    selected = set(names)
    unknown = selected - {tool.name for tool in tools}
    if unknown:
        raise KeyError(f"未知工具: {', '.join(sorted(unknown))}")
    return [tool for tool in tools if tool.name in selected]


def build_dispatch_node(tools: Iterable[BaseTool]) -> ToolNode:
    return ToolNode(list(tools), handle_tool_errors=True)


def route_after_assistant(state: MessagesState) -> Literal["tools", "__end__"]:
    last_message = state["messages"][-1]
    if getattr(last_message, "tool_calls", None):
        return "tools"
    return END
