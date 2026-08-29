"""Alibaba Cloud IQS-backed, source-preserving external web search."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from langchain_core.tools import BaseTool, tool

from app.config import Settings, get_settings

SearchTopic = Literal["general", "news", "finance"]
TimeRange = Literal["day", "week", "month", "year"]

_TIME_RANGES: dict[TimeRange, str] = {
    "day": "OneDay",
    "week": "OneWeek",
    "month": "OneMonth",
    "year": "OneYear",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value.strip()


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _credit_total(value: Any) -> float | None:
    numbers: list[float] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, int | float) and not isinstance(item, bool):
            numbers.append(float(item))

    visit(value)
    return sum(numbers) if numbers else None


class IqsSearchService:
    """Bounded IQS UnifiedSearch adapter with sanitized failures and usage metadata."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        topic: SearchTopic = "general",
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        normalized_query = " ".join(query.split())
        limit = max(1, min(max_results, self.settings.iqs_max_results, 50))
        if not normalized_query:
            return self._error(
                normalized_query,
                limit,
                "invalid_query",
                "搜索关键词不能为空。",
                retryable=False,
            )
        if self.settings.web_search_provider != "iqs":
            return self._not_configured(normalized_query, limit)
        secret = self.settings.iqs_api_key
        if secret is None or not secret.get_secret_value():
            return self._not_configured(normalized_query, limit)

        body: dict[str, Any] = {
            "query": normalized_query[:500],
            "engineType": self.settings.iqs_engine_type,
            "timeRange": _TIME_RANGES.get(time_range, "NoLimit"),
            "contents": {
                "mainText": False,
                "markdownText": False,
                "summary": False,
                "rerankScore": True,
            },
            "advancedParams": {"numResults": str(limit)},
        }
        if topic == "finance":
            body["category"] = "finance"

        headers = {
            "Authorization": f"Bearer {secret.get_secret_value()}",
            "Content-Type": "application/json",
            "User-Agent": "globuy/0.1",
        }
        endpoint = f"{self.settings.iqs_base_url.rstrip('/')}/search/unified"
        try:
            if self.client is not None:
                response = await self.client.post(endpoint, json=body, headers=headers)
            else:
                timeout = httpx.Timeout(self.settings.iqs_timeout_seconds)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(endpoint, json=body, headers=headers)
        except (TimeoutError, httpx.TimeoutException):
            return self._error(
                normalized_query,
                limit,
                "timeout",
                "阿里云 IQS 搜索超时。",
                retryable=True,
            )
        except httpx.HTTPError:
            return self._error(
                normalized_query,
                limit,
                "provider_unavailable",
                "阿里云 IQS 搜索服务暂时不可用。",
                retryable=True,
            )

        if response.status_code != 200:
            return self._http_error(normalized_query, limit, response)
        try:
            payload = response.json()
        except ValueError:
            return self._error(
                normalized_query,
                limit,
                "invalid_response",
                "阿里云 IQS 返回了无法解析的响应。",
                retryable=True,
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("pageItems"), list):
            return self._error(
                normalized_query,
                limit,
                "invalid_response",
                "阿里云 IQS 响应缺少 pageItems。",
                retryable=True,
            )

        results: list[dict[str, Any]] = []
        for raw in payload["pageItems"][:limit]:
            if not isinstance(raw, dict):
                continue
            url = _safe_url(raw.get("link"))
            title = raw.get("title")
            if url is None or not isinstance(title, str) or not title.strip():
                continue
            snippet = raw.get("snippet") if isinstance(raw.get("snippet"), str) else ""
            results.append(
                {
                    "title": title.strip()[:500],
                    "url": url,
                    "content": snippet.strip()[: self.settings.web_search_content_chars],
                    "score": _number(raw.get("rerankScore")),
                    "published_date": (
                        raw.get("publishedTime")
                        if isinstance(raw.get("publishedTime"), str)
                        else None
                    ),
                }
            )

        search_information = (
            payload.get("searchInformation")
            if isinstance(payload.get("searchInformation"), dict)
            else {}
        )
        search_time_ms = _number(search_information.get("searchTime"))
        return {
            "status": "ok",
            "provider": "iqs",
            "query": normalized_query,
            "max_results": limit,
            "results": results,
            "result_count": len(results),
            "retrieved_at": _now(),
            "response_time_seconds": (
                search_time_ms / 1_000 if search_time_ms is not None else None
            ),
            "request_id": (
                payload.get("requestId")
                if isinstance(payload.get("requestId"), str)
                else None
            ),
            "credits_used": _credit_total(payload.get("costCredits")),
            "source_kind": "web",
        }

    @staticmethod
    def _not_configured(query: str, limit: int) -> dict[str, Any]:
        return {
            "status": "not_configured",
            "provider": "iqs",
            "query": query,
            "max_results": limit,
            "results": [],
            "message": "阿里云 IQS WebSearch 尚未配置有效 API Key。",
        }

    @staticmethod
    def _error(
        query: str,
        limit: int,
        code: str,
        message: str,
        *,
        retryable: bool,
    ) -> dict[str, Any]:
        return {
            "status": "error",
            "provider": "iqs",
            "query": query,
            "max_results": limit,
            "results": [],
            "error": {"code": code, "message": message, "retryable": retryable},
        }

    def _http_error(
        self,
        query: str,
        limit: int,
        response: httpx.Response,
    ) -> dict[str, Any]:
        provider_code = ""
        try:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("code"), str):
                provider_code = payload["code"]
        except ValueError:
            pass

        if response.status_code in {401, 404}:
            return self._error(
                query,
                limit,
                "authentication_failed",
                "阿里云 IQS API Key 无效或不存在。",
                retryable=False,
            )
        if response.status_code == 403:
            code = "usage_limit" if provider_code == "Retrieval.Arrears" else "access_denied"
            return self._error(
                query,
                limit,
                code,
                "阿里云 IQS 服务未开通、未授权、试用到期或账户余额不足。",
                retryable=False,
            )
        if response.status_code == 429:
            exceeded = provider_code == "Retrieval.TestUserQueryExceeded"
            return self._error(
                query,
                limit,
                "usage_limit" if exceeded else "rate_limited",
                "阿里云 IQS 已达到调用量或速率限制。",
                retryable=not exceeded,
            )
        return self._error(
            query,
            limit,
            "provider_error",
            "阿里云 IQS 搜索请求失败。",
            retryable=response.status_code >= 500,
        )


def build_web_search_tool(service: IqsSearchService | None = None) -> BaseTool:
    search_service = service or IqsSearchService()

    @tool("web_search")
    async def iqs_web_search(
        query: str,
        max_results: int = 5,
        topic: SearchTopic = "general",
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """Search current web sources with Alibaba Cloud IQS and return cited snippets."""

        return await search_service.search(
            query,
            max_results=max_results,
            topic=topic,
            time_range=time_range,
        )

    return iqs_web_search


web_search = build_web_search_tool()

__all__ = ["IqsSearchService", "build_web_search_tool", "web_search"]
