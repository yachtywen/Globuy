"""External research boundary.

The provider adapter is intentionally not hard-coded. A later chapter can
connect Tavily, SerpAPI, Bing, or an internal search service here.
"""

from langchain_core.tools import tool


@tool
def web_search(query: str, max_results: int = 5) -> dict:
    """Search external sources; currently reports that no provider is configured."""

    return {
        "status": "not_configured",
        "query": query.strip(),
        "max_results": max(1, min(max_results, 10)),
        "results": [],
        "message": "尚未配置 Web Search provider，不能生成未经核验的外部资料。",
    }
