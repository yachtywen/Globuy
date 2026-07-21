"""Nine core shopping tools and their runtime registry."""

from collections.abc import Iterable

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from app.tools.category_insight import category_insight
from app.tools.chat_fallback import chat_fallback
from app.tools.item_picker import item_picker
from app.tools.item_search import item_search
from app.tools.planner import planner
from app.tools.price_compare import price_compare
from app.tools.shipping_calc import shipping_calc
from app.tools.shopping_summary import build_shopping_summary_tool
from app.tools.web_search import web_search

CORE_TOOL_NAMES: tuple[str, ...] = (
    "planner",
    "chat_fallback",
    "web_search",
    "category_insight",
    "item_search",
    "item_picker",
    "price_compare",
    "shipping_calc",
    "shopping_summary",
)

TOOL_PHASES: dict[str, frozenset[str]] = {
    "think": frozenset(
        {
            "planner",
            "chat_fallback",
            "web_search",
            "category_insight",
            "item_search",
            "dispatch_tool",
        }
    ),
    "reflect": frozenset(
        {
            "category_insight",
            "price_compare",
            "shipping_calc",
            "item_picker",
            "shopping_summary",
            "chat_fallback",
        }
    ),
}
TERMINAL_TOOLS = frozenset({"shopping_summary", "chat_fallback"})


def build_core_tools(model: BaseChatModel | None = None) -> tuple[BaseTool, ...]:
    """Build the immutable nine-tool business set for one homogeneous family."""

    return (
        planner,
        chat_fallback,
        web_search,
        category_insight,
        item_search,
        item_picker,
        price_compare,
        shipping_calc,
        build_shopping_summary_tool(model),
    )


# Compatibility registry for non-Agent callers. Its Summary tool is explicitly not configured.
CORE_TOOLS: tuple[BaseTool, ...] = build_core_tools(None)


class ToolRegistry:
    def __init__(self, tools: Iterable[BaseTool] = ()) -> None:
        self._tools = {registered.name: registered for registered in tools}

    def register(self, registered: BaseTool) -> None:
        if registered.name in self._tools:
            raise ValueError(f"工具已注册: {registered.name}")
        self._tools[registered.name] = registered

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"未知工具: {name}") from exc

    def all(self) -> list[BaseTool]:
        return list(self._tools.values())


tool_registry = ToolRegistry(CORE_TOOLS)

__all__ = [
    "CORE_TOOL_NAMES",
    "CORE_TOOLS",
    "TERMINAL_TOOLS",
    "TOOL_PHASES",
    "ToolRegistry",
    "build_core_tools",
    "tool_registry",
]
