"""Cross-platform product-search boundary."""

from langchain_core.tools import tool


@tool
def item_search(query: str, platforms: list[str] | None = None) -> dict:
    """Search configured commerce platforms for product candidates."""

    return {
        "status": "not_configured",
        "query": query.strip(),
        "platforms": platforms or [],
        "items": [],
        "message": "尚未接入电商平台数据源，不能编造商品或实时价格。",
    }
