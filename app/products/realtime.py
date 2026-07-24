"""Truthful runtime product-provider retrieval with a short-lived candidate cache."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol

import httpx

from app.config import Settings, get_settings
from app.search.schemas import Candidate, Platform

_JUSTONE_ENDPOINTS: dict[Platform, str] = {
    "taobao": "/api/taobao/search-item-list/v1",
    "jingdong": "/api/jd/search-item-list/v1",
    "douyin": "/api/douyin-ec/search-item-list/v1",
}


class RealtimeProviderError(RuntimeError):
    pass


class RealtimeProviderNotConfigured(RealtimeProviderError):
    pass


class RealtimeProductProvider(Protocol):
    async def search(self, query: str, platform: Platform, limit: int) -> list[Candidate]: ...


def _number(value: Any) -> float | None:
    if value in (None, "", -1, "-1"):
        return None
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return float(parsed) if parsed >= 0 else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _absolute_url(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if text.startswith("//"):
        return f"https:{text}"
    return text if text.startswith(("http://", "https://")) else None


def _jd_image_url(value: Any) -> str | None:
    """Normalize the relative JFS paths returned by the JD search endpoint."""

    url = _absolute_url(value)
    if url:
        return url
    if not value:
        return None
    path = str(value).strip().lstrip("/")
    return f"https://img10.360buyimg.com/n1/{path}" if path.startswith("jfs/") else None


def _items(payload: dict[str, Any], platform: Platform) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    if platform == "taobao":
        model = data.get("model")
        rows = model.get("itemList") if isinstance(model, dict) else []
    elif platform == "jingdong":
        rows = data.get("products")
    else:
        rows = data.get("summary_promotions") or data.get("promotions")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _candidate(row: dict[str, Any], platform: Platform, data_as_of: str) -> Candidate | None:
    if platform == "taobao":
        source_id = row.get("itemId") or row.get("prodId")
        title = row.get("itemName") or row.get("itemSubName")
        price = _number(row.get("discntPriceYuan") or row.get("priceZKYuanDouble") or row.get("priceYuanDouble"))
        image = _absolute_url(row.get("picUrlFull") or row.get("picUrl"))
        url = f"https://item.taobao.com/item.htm?id={source_id}" if source_id else None
        attributes = {key: value for key, value in {"shop_name": row.get("shopName"), "shop_id": row.get("shopId")}.items() if value not in (None, "")}
        rating, sales = _number(row.get("itemGradeAvg")), _integer(row.get("orderPayUV"))
    elif platform == "jingdong":
        source_id = row.get("id") or row.get("oneItemId")
        title, price = row.get("title") or row.get("shortTitle"), _number(row.get("price") or row.get("lowestPrice"))
        image = _jd_image_url(row.get("imageUrl") or row.get("longImageUrl"))
        url = _absolute_url(row.get("landUrl")) or (f"https://item.jd.com/{source_id}.html" if source_id else None)
        attributes = {key: value for key, value in {"shop_name": row.get("shopName"), "shop_id": row.get("shopId"), "is_self_operated": row.get("zy")}.items() if value not in (None, "")}
        rating, sales = None, _integer(row.get("sales") or row.get("monthSales"))
    else:
        base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
        product = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
        marketing = base.get("marketing_info") if isinstance(base.get("marketing_info"), dict) else {}
        price_data = marketing.get("price_desc") if isinstance(marketing.get("price_desc"), dict) else {}
        source_id, title = row.get("product_id") or row.get("promotion_id"), product.get("name")
        cents = _number(price_data.get("price", {}).get("origin") if isinstance(price_data.get("price"), dict) else price_data.get("price"))
        price = cents / 100 if cents is not None else None
        image, url = _absolute_url(product.get("main_img") or product.get("white_img")), _absolute_url(product.get("detail_url"))
        shop = base.get("shop_info") if isinstance(base.get("shop_info"), dict) else {}
        attributes = {key: value for key, value in {"shop_name": shop.get("shop_name"), "shop_id": shop.get("shop_id")}.items() if value not in (None, "")}
        rating, sales = None, _integer(product.get("month_sale", {}).get("origin") if isinstance(product.get("month_sale"), dict) else product.get("month_sale"))
    if not source_id or not title or price is None:
        return None
    return Candidate(item_id=f"{platform}:{source_id}", platform=platform, title=str(title).strip(), price=price, currency="CNY", rating=rating, sales=sales, image_url=image, attributes=attributes, product_url=url, retrieval_rank=1, source_kind="realtime_provider", data_as_of=data_as_of)


class JustOneRealtimeProvider:
    def __init__(self, settings: Settings) -> None:
        if settings.justone_api_token is None:
            raise RealtimeProviderNotConfigured("实时商品 Provider 未配置令牌")
        self.settings = settings

    async def search(self, query: str, platform: Platform, limit: int) -> list[Candidate]:
        endpoint = _JUSTONE_ENDPOINTS[platform]
        params: dict[str, Any] = {"keyword": query, "page": 1, "pageSize": limit, "token": self.settings.justone_api_token.get_secret_value()}
        if platform == "jingdong":
            params = {"keyword": query, "page": 1, "pageSize": limit, "token": self.settings.justone_api_token.get_secret_value()}
        try:
            async with httpx.AsyncClient(base_url=self.settings.justone_base_url, timeout=self.settings.realtime_search_timeout_seconds, headers={"Accept": "application/json"}) as client:
                # Just One documents 301 as an intermittent collection failure.
                # Retry it once only; this avoids hiding a persistent provider fault
                # or generating an unbounded number of billable requests.
                payload: Any = None
                for attempt in range(2):
                    response = await client.get(endpoint, params=params)
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or str(payload.get("code", "0")) != "301":
                        break
                    if attempt == 0:
                        await asyncio.sleep(0.25)
        except httpx.HTTPError as exc:
            raise RealtimeProviderError(f"实时 Provider 请求失败：{type(exc).__name__}") from exc
        if not isinstance(payload, dict) or str(payload.get("code", "0")) != "0":
            code = str(payload.get("code", "unknown")) if isinstance(payload, dict) else "invalid_response"
            raise RealtimeProviderError(f"实时 Provider 返回业务错误：{code}")
        captured = datetime.now(UTC).isoformat()
        result = [candidate for row in _items(payload, platform) if (candidate := _candidate(row, platform, captured))]
        return [candidate.model_copy(update={"retrieval_rank": index}) for index, candidate in enumerate(result[:limit], 1)]


@dataclass
class _CacheEntry:
    expires_at: float
    candidates: list[Candidate]
    data_as_of: str


class RealtimeCandidateCache:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, Platform], _CacheEntry] = {}

    def get(self, query: str, platform: Platform) -> _CacheEntry | None:
        entry = self._entries.get((query.strip().casefold(), platform))
        return entry if entry and entry.expires_at > time.monotonic() else None

    def put(self, query: str, platform: Platform, candidates: list[Candidate], ttl_seconds: int) -> _CacheEntry:
        data_as_of = datetime.now(UTC).isoformat()
        entry = _CacheEntry(time.monotonic() + ttl_seconds, candidates, data_as_of)
        self._entries[(query.strip().casefold(), platform)] = entry
        return entry


def build_realtime_provider(settings: Settings | None = None) -> RealtimeProductProvider:
    settings = settings or get_settings()
    if settings.realtime_product_provider != "justone":
        raise RealtimeProviderNotConfigured("实时商品 Provider 尚未启用")
    return JustOneRealtimeProvider(settings)
