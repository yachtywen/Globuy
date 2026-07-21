"""Tavily-backed, source-preserving external web search."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from langchain_core.tools import BaseTool, tool

from app.config import Settings, get_settings

SearchTopic = Literal["general", "news", "finance"]
TimeRange = Literal["day", "week", "month", "year"]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value.strip()


def _clean_domains(values: list[str] | None, *, limit: int) -> list[str]:
    cleaned: list[str] = []
    for value in values or []:
        domain = value.strip().lower()
        if not domain or "/" in domain or ":" in domain or domain in cleaned:
            continue
        cleaned.append(domain[:253])
        if len(cleaned) >= limit:
            break
    return cleaned


class TavilySearchService:
    """Small HTTP adapter with explicit credit, timeout, and truthfulness boundaries."""

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
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_query = " ".join(query.split())
        limit = max(1, min(max_results, self.settings.tavily_max_results, 20))
        if not normalized_query:
            return self._error(
                normalized_query,
                limit,
                "invalid_query",
                "搜索关键词不能为空。",
                retryable=False,
            )
        if self.settings.web_search_provider != "tavily":
            return self._not_configured(normalized_query, limit)
        secret = self.settings.tavily_api_key
        if secret is None or not secret.get_secret_value():
            return self._not_configured(normalized_query, limit)

        body: dict[str, Any] = {
            "query": normalized_query[:2_000],
            "search_depth": self.settings.tavily_search_depth,
            "topic": topic,
            "max_results": limit,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "auto_parameters": False,
        }
        if time_range is not None:
            body["time_range"] = time_range
        included = _clean_domains(include_domains, limit=20)
        excluded = _clean_domains(exclude_domains, limit=20)
        if included:
            body["include_domains"] = included
        if excluded:
            body["exclude_domains"] = excluded

        headers = {
            "Authorization": f"Bearer {secret.get_secret_value()}",
            "Content-Type": "application/json",
            "User-Agent": "globuy/0.1",
        }
        if self.settings.tavily_project_id:
            headers["X-Project-ID"] = self.settings.tavily_project_id
        endpoint = f"{self.settings.tavily_base_url.rstrip('/')}/search"
        try:
            if self.client is not None:
                response = await self.client.post(endpoint, json=body, headers=headers)
            else:
                timeout = httpx.Timeout(self.settings.tavily_timeout_seconds)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(endpoint, json=body, headers=headers)
        except TimeoutError:
            return self._error(
                normalized_query,
                limit,
                "timeout",
                "Tavily 搜索超时。",
                retryable=True,
            )
        except httpx.TimeoutException:
            return self._error(
                normalized_query,
                limit,
                "timeout",
                "Tavily 搜索超时。",
                retryable=True,
            )
        except httpx.HTTPError:
            return self._error(
                normalized_query,
                limit,
                "provider_unavailable",
                "Tavily 搜索服务暂时不可用。",
                retryable=True,
            )

        if response.status_code != 200:
            return self._http_error(normalized_query, limit, response.status_code)
        try:
            payload = response.json()
        except ValueError:
            return self._error(
                normalized_query,
                limit,
                "invalid_response",
                "Tavily 返回了无法解析的响应。",
                retryable=True,
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            return self._error(
                normalized_query,
                limit,
                "invalid_response",
                "Tavily 响应缺少结果列表。",
                retryable=True,
            )

        results: list[dict[str, Any]] = []
        for raw in payload["results"][:limit]:
            if not isinstance(raw, dict):
                continue
            url = _safe_url(raw.get("url"))
            title = raw.get("title")
            if url is None or not isinstance(title, str) or not title.strip():
                continue
            content = raw.get("content") if isinstance(raw.get("content"), str) else ""
            score = raw.get("score")
            results.append(
                {
                    "title": title.strip()[:500],
                    "url": url,
                    "content": content.strip()[: self.settings.web_search_content_chars],
                    "score": float(score) if isinstance(score, int | float) else None,
                    "published_date": (
                        raw.get("published_date")
                        if isinstance(raw.get("published_date"), str)
                        else None
                    ),
                }
            )
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        credits = usage.get("credits")
        return {
            "status": "ok",
            "provider": "tavily",
            "query": normalized_query,
            "max_results": limit,
            "results": results,
            "result_count": len(results),
            "retrieved_at": _now(),
            "response_time_seconds": self._number(payload.get("response_time")),
            "request_id": (
                payload.get("request_id")
                if isinstance(payload.get("request_id"), str)
                else None
            ),
            "credits_used": self._number(credits),
            "source_kind": "web",
        }

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _not_configured(query: str, limit: int) -> dict[str, Any]:
        return {
            "status": "not_configured",
            "provider": "tavily",
            "query": query,
            "max_results": limit,
            "results": [],
            "message": "Tavily WebSearch 尚未配置有效 API key。",
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
            "provider": "tavily",
            "query": query,
            "max_results": limit,
            "results": [],
            "error": {"code": code, "message": message, "retryable": retryable},
        }

    def _http_error(self, query: str, limit: int, status_code: int) -> dict[str, Any]:
        if status_code in {401, 403}:
            return self._error(
                query,
                limit,
                "authentication_failed",
                "Tavily API 认证失败，请检查或轮换 API key。",
                retryable=False,
            )
        if status_code == 429:
            return self._error(
                query,
                limit,
                "rate_limited",
                "Tavily API 已达到速率限制。",
                retryable=True,
            )
        if status_code in {432, 433}:
            return self._error(
                query,
                limit,
                "usage_limit",
                "Tavily 账户额度或计划限制阻止了本次搜索。",
                retryable=False,
            )
        return self._error(
            query,
            limit,
            "provider_error",
            "Tavily 搜索请求失败。",
            retryable=status_code >= 500,
        )


def build_web_search_tool(service: TavilySearchService | None = None) -> BaseTool:
    search_service = service or TavilySearchService()

    @tool("web_search")
    async def tavily_web_search(
        query: str,
        max_results: int = 5,
        topic: SearchTopic = "general",
        time_range: TimeRange | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """Search current web sources with Tavily and return cited snippets, not invented facts."""

        return await search_service.search(
            query,
            max_results=max_results,
            topic=topic,
            time_range=time_range,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
        )

    return tavily_web_search


web_search = build_web_search_tool()

__all__ = ["TavilySearchService", "build_web_search_tool", "web_search"]
