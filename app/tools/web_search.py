"""Tavily-backed, source-preserving external web search."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import httpx
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import InjectedState

from app.config import Settings, get_settings

SearchTopic = Literal["general", "news", "finance"]
TimeRange = Literal["day", "week", "month", "year"]
SearchMode = Literal["general", "product_reviews"]

_REVIEW_DOMAINS = ("xiaohongshu.com", "zhihu.com")
_REVIEW_SOURCE_NAMES = {
    "xiaohongshu.com": "小红书",
    "zhihu.com": "知乎",
}
_REVIEW_INTENT_PHRASES = tuple(
    sorted(
        {
            "帮我了解一下",
            "我想了解一下",
            "值不值得购买",
            "值不值得买",
            "使用体验",
            "真实体验",
            "有什么优缺点",
            "优缺点",
            "口碑怎么样",
            "评价怎么样",
            "表现怎么样",
            "怎么样",
            "了解一下",
            "了解",
            "看看",
            "测评",
            "评测",
            "评价",
            "口碑",
            "这款商品",
            "这个商品",
            "这款产品",
            "这个产品",
        },
        key=len,
        reverse=True,
    )
)
_GENERIC_REVIEW_TERMS = {
    "商品",
    "产品",
    "东西",
    "型号",
    "耳机",
    "手机",
    "电脑",
    "相机",
    "测评",
    "评测",
    "评价",
    "口碑",
    "体验",
    "review",
    "reviews",
    "product",
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


def _review_source(url: str) -> str | None:
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    for domain, source_name in _REVIEW_SOURCE_NAMES.items():
        if hostname == domain or hostname.endswith(f".{domain}"):
            return source_name
    return None


def _markdown_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _normalized_relevance_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _compact_relevance_text(value: str) -> str:
    return "".join(
        character
        for character in _normalized_relevance_text(value)
        if character.isalnum() or "\u4e00" <= character <= "\u9fff"
    )


def _review_relevance_profile(query: str) -> dict[str, Any]:
    cleaned = _normalized_relevance_text(query)
    for phrase in _REVIEW_INTENT_PHRASES:
        cleaned = cleaned.replace(phrase, " ")

    raw_latin_terms = re.findall(r"[a-z0-9]+(?:[-_.+][a-z0-9]+)*", cleaned)
    cjk_terms = re.findall(r"[\u4e00-\u9fff]{2,}", cleaned)
    model_terms: set[str] = set()
    word_terms: set[str] = set()
    numeric_terms: set[str] = set()

    for raw_term in raw_latin_terms:
        compact = _compact_relevance_text(raw_term)
        if not compact:
            continue
        has_letter = any(character.isalpha() for character in compact)
        has_digit = any(character.isdigit() for character in compact)
        if has_letter and has_digit and len(compact) >= 2:
            model_terms.add(compact)
            for part in re.findall(r"[a-z0-9]+", raw_term):
                part_compact = _compact_relevance_text(part)
                if (
                    len(part_compact) >= 3
                    and any(character.isalpha() for character in part_compact)
                    and any(character.isdigit() for character in part_compact)
                ):
                    model_terms.add(part_compact)
            suffix = re.search(r"[a-z]{1,4}\d{1,4}$", compact)
            if suffix is not None and len(suffix.group()) >= 3:
                model_terms.add(suffix.group())
        elif compact.isdigit():
            if len(compact) >= 2:
                numeric_terms.add(compact)
                word_terms.add(compact)
        elif len(compact) >= 2 and compact not in _GENERIC_REVIEW_TERMS:
            word_terms.add(compact)

    for term in cjk_terms:
        compact = _compact_relevance_text(term)
        if compact and compact not in _GENERIC_REVIEW_TERMS:
            word_terms.add(compact)

    phrase = _compact_relevance_text(cleaned)
    return {
        "phrase": phrase,
        "model_terms": sorted(model_terms, key=lambda value: (-len(value), value)),
        "numeric_terms": sorted(numeric_terms),
        "word_terms": sorted(word_terms, key=lambda value: (-len(value), value)),
    }


def _review_relevance_evidence(query: str, result: dict[str, Any]) -> list[str]:
    profile = _review_relevance_profile(query)
    searchable = _compact_relevance_text(
        f'{result.get("title") or ""} {result.get("content") or ""}'
    )
    if not searchable:
        return []

    model_matches = [term for term in profile["model_terms"] if term in searchable]
    if profile["model_terms"] and not model_matches:
        return []
    numeric_matches = [term for term in profile["numeric_terms"] if term in searchable]
    if profile["numeric_terms"] and not numeric_matches:
        return []

    word_matches = [term for term in profile["word_terms"] if term in searchable]
    phrase = profile["phrase"]
    phrase_matched = len(phrase) >= 4 and phrase in searchable
    minimum_word_matches = min(2, len(profile["word_terms"]))
    enough_words = minimum_word_matches > 0 and len(word_matches) >= minimum_word_matches
    if not (model_matches or phrase_matched or enough_words):
        return []
    return list(dict.fromkeys([*model_matches, *numeric_matches, *word_matches]))


def _feedback_candidates_from_state(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read only previously observed product facts; never infer review text."""

    if not isinstance(state, dict):
        return []
    messages = state.get("messages")
    if not isinstance(messages, list):
        return []
    for message in reversed(messages):
        name = getattr(message, "name", None)
        if name not in {"item_picker", "item_search", "shopping_summary", "dispatch_tool"}:
            continue
        content = getattr(message, "content", None)
        if isinstance(content, str):
            try:
                payload = json.loads(content)
            except (TypeError, ValueError):
                continue
        elif isinstance(content, dict):
            payload = content
        else:
            continue
        if not isinstance(payload, dict):
            continue
        picks = payload.get("picks") or payload.get("candidates")
        if not isinstance(picks, list) and name == "dispatch_tool":
            picks = [
                candidate
                for result in payload.get("search_results", [])
                if isinstance(result, dict)
                for candidate in (result.get("candidates") or [])
            ]
        if isinstance(picks, list):
            valid = [item for item in picks if isinstance(item, dict)]
            if valid:
                return valid
    return []


def _aggregate_feedback(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    feedback: list[dict[str, Any]] = []
    seen: set[str] = set()
    labels = {"taobao": "淘宝", "jingdong": "京东", "douyin": "抖音"}
    for candidate in candidates:
        platform = str(candidate.get("platform") or "").strip().lower()
        title = str(candidate.get("title") or "").strip()
        item_id = str(candidate.get("item_id") or "").strip()
        if platform not in labels or not title or not item_id or item_id in seen:
            continue
        attributes = candidate.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        rating = candidate.get("rating")
        comment_count = attributes.get("comment_count")
        rating_type = attributes.get("rating_type")
        if rating is None and comment_count is None:
            continue
        seen.add(item_id)
        feedback.append(
            {
                "rank": len(feedback) + 1,
                "platform": platform,
                "platform_name": labels[platform],
                "item_id": item_id,
                "title": title[:500],
                "rating": rating if isinstance(rating, int | float) else None,
                "rating_scale": 100 if rating_type == "good_ratio_percent" else 5,
                "rating_type": rating_type or "average_rating",
                "comment_count": comment_count if isinstance(comment_count, int | float) else None,
                "product_url": (
                    candidate.get("product_url")
                    if isinstance(candidate.get("product_url"), str)
                    else None
                ),
                "data_as_of": (
                    candidate.get("data_as_of")
                    if isinstance(candidate.get("data_as_of"), str)
                    else None
                ),
                "aggregate_only": True,
            }
        )
        if len(feedback) == 3:
            break
    return feedback


def _feedback_text(query: str, feedback: list[dict[str, Any]]) -> str:
    lines = [
        f"未在小红书或知乎找到明确提及“{_markdown_text(query)}”的测评。",
        "以下是此前已核验购物平台候选中的消费者评价聚合信号，不是消费者评价原文：",
        "",
    ]
    for item in feedback:
        rating = item.get("rating")
        rating_text = (
            f"评分 {rating:g}/{item['rating_scale']}"
            if isinstance(rating, (int, float))
            else "评分未提供"
        )
        comments = item.get("comment_count")
        comment_text = (
            f"评论数 {int(comments):,}"
            if isinstance(comments, (int, float))
            else "评论数未提供"
        )
        title = _markdown_text(item["title"])
        link = item.get("product_url")
        suffix = f" · [查看商品]({link})" if isinstance(link, str) and _safe_url(link) else ""
        lines.append(
            f"{item['rank']}. {item['platform_name']} · {title} · "
            f"{rating_text} · {comment_text}{suffix}"
        )
    return "\n".join(lines)


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

    async def search_product_reviews(
        self,
        query: str,
        *,
        time_range: TimeRange | None = None,
        platform_candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return at most three deduplicated Xiaohongshu/Zhihu review results."""

        normalized_query = " ".join(query.split())
        if not normalized_query:
            return self._review_terminal(
                normalized_query,
                self._error(
                    normalized_query,
                    3,
                    "invalid_query",
                    "商品名称或型号不能为空。",
                    retryable=False,
                ),
            )
        search_query = f'"{normalized_query}" 测评 评价 使用体验 优缺点'
        provider_limit = max(3, min(self.settings.tavily_max_results, 10))
        raw_result = await self.search(
            search_query,
            max_results=provider_limit,
            topic="general",
            time_range=time_range,
            include_domains=list(_REVIEW_DOMAINS),
        )
        return self._review_terminal(
            normalized_query,
            raw_result,
            platform_feedback=_aggregate_feedback(platform_candidates or []),
        )

    @staticmethod
    def _review_terminal(
        query: str,
        raw_result: dict[str, Any],
        *,
        platform_feedback: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        raw_status = raw_result.get("status")
        if raw_status != "ok":
            error = raw_result.get("error") if isinstance(raw_result.get("error"), dict) else {}
            message = str(
                raw_result.get("message")
                or error.get("message")
                or "暂时无法检索小红书或知乎的商品测评。"
            )
            feedback = platform_feedback or []
            if feedback:
                return {
                    **raw_result,
                    "status": "incomplete",
                    "query": query,
                    "search_mode": "product_reviews",
                    "source_kind": "platform_feedback",
                    "results": [],
                    "review_results": [],
                    "platform_feedback": feedback,
                    "result_count": 0,
                    "terminal": True,
                    "final_text": (
                        f"小红书或知乎暂时无法检索。\n\n{_feedback_text(query, feedback)}"
                    ),
                    "picks": [],
                    "unresolved": ["未取得小红书或知乎测评正文；购物平台数据仅为聚合评价信号"],
                }
            return {
                **raw_result,
                "status": "not_configured" if raw_status == "not_configured" else "error",
                "query": query,
                "search_mode": "product_reviews",
                "source_kind": "content_review",
                "results": [],
                "review_results": [],
                "result_count": 0,
                "terminal": True,
                "final_text": message,
                "picks": [],
                "unresolved": [message],
            }

        reviews: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        discarded_irrelevant_count = 0
        for raw in raw_result.get("results", []):
            if not isinstance(raw, dict):
                continue
            url = raw.get("url")
            if not isinstance(url, str) or url in seen_urls:
                continue
            source = _review_source(url)
            if source is None:
                continue
            relevance_evidence = _review_relevance_evidence(query, raw)
            if not relevance_evidence:
                discarded_irrelevant_count += 1
                continue
            seen_urls.add(url)
            reviews.append(
                {
                    **raw,
                    "rank": len(reviews) + 1,
                    "source": source,
                    "relevance_evidence": relevance_evidence,
                }
            )
            if len(reviews) == 3:
                break

        if reviews:
            lines = ["## 小红书 / 知乎测评 Top 3", ""]
            for item in reviews:
                title = _markdown_text(str(item["title"]))
                lines.append(f'{item["rank"]}. [{title}]({item["url"]}) · {item["source"]}')
                excerpt = _markdown_text(
                    " ".join(str(item.get("content") or "").split())[:180]
                )
                if excerpt:
                    lines.append(f"   摘要：{excerpt}")
            final_text = "\n".join(lines)
        elif platform_feedback:
            final_text = _feedback_text(query, platform_feedback)
        else:
            final_text = (
                f"本次未在小红书或知乎找到明确提及“{_markdown_text(query)}”的可核验测评。"
                "为避免答非所问，已过滤不相关页面。"
            )

        complete = len(reviews) == 3
        unresolved = [] if complete else [f"仅检索到 {len(reviews)} 条目标平台有效结果"]
        return {
            **raw_result,
            "status": "complete" if complete else "incomplete",
            "query": query,
            "search_query": raw_result.get("query"),
            "search_mode": "product_reviews",
            "source_kind": (
                "content_review"
                if reviews
                else "platform_feedback"
                if platform_feedback
                else "content_review"
            ),
            "max_results": 3,
            "results": reviews,
            "review_results": reviews,
            "platform_feedback": platform_feedback or [],
            "result_count": len(reviews),
            "discarded_irrelevant_count": discarded_irrelevant_count,
            "terminal": True,
            "final_text": final_text,
            "picks": [],
            "unresolved": (
                unresolved
                if reviews
                else [
                    "未检索到小红书或知乎测评正文；购物平台仅提供聚合评价信号"
                ]
                if platform_feedback
                else unresolved
            ),
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
        search_mode: SearchMode = "general",
        state: Annotated[dict[str, Any], InjectedState] = None,
    ) -> dict[str, Any]:
        """Search cited web sources; use product_reviews for Xiaohongshu/Zhihu Top-3."""

        if search_mode == "product_reviews":
            return await search_service.search_product_reviews(
                query,
                time_range=time_range,
                platform_candidates=_feedback_candidates_from_state(state),
            )

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

__all__ = ["SearchMode", "TavilySearchService", "build_web_search_tool", "web_search"]
