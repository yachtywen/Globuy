"""Nine core shopping tools and their central registry."""

from collections.abc import Iterable

from langchain_core.tools import BaseTool

from app.tools.category_insight import category_insight
from app.tools.chat_fallback import chat_fallback
from app.tools.item_picker import item_picker
from app.tools.item_search import item_search
from app.tools.planner import planner
from app.tools.price_compare import price_compare
from app.tools.shipping_calc import shipping_calc
from app.tools.shopping_summary import shopping_summary
from app.tools.web_search import web_search

CORE_TOOLS: tuple[BaseTool, ...] = (
    planner,
    chat_fallback,
    web_search,
    category_insight,
    item_search,
    item_picker,
    price_compare,
    shipping_calc,
    shopping_summary,
)


class ToolRegistry:
    def __init__(self, tools: Iterable[BaseTool] = ()) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具已注册: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"未知工具: {name}") from exc

    def all(self) -> list[BaseTool]:
        return list(self._tools.values())


tool_registry = ToolRegistry(CORE_TOOLS)

__all__ = ["CORE_TOOLS", "ToolRegistry", "tool_registry"]
