"""Asynchronous, budget-safe Just One API search adapter."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.products.providers.base import (
    ProductProvider,
    ProviderErrorCode,
    ProviderPage,
    ProviderSearchRequest,
)
from app.products.providers.normalization import extract_items

ENDPOINTS = {
    "taobao": "/api/taobao/search-item-list/v1",
    "jingdong": "/api/jd/search-item-list/v1",
    "douyin": "/api/douyin-ec/search-item-list/v1",
}

BUSINESS_STATUS = {
    "0": ProviderErrorCode.OK,
    "100": ProviderErrorCode.NOT_CONFIGURED,
    "301": ProviderErrorCode.PROVIDER_ERROR,
    "302": ProviderErrorCode.RATE_LIMITED,
    "303": ProviderErrorCode.QUOTA_EXCEEDED,
    "400": ProviderErrorCode.INVALID_REQUEST,
    "500": ProviderErrorCode.PROVIDER_ERROR,
    "600": ProviderErrorCode.NOT_CONFIGURED,
    "601": ProviderErrorCode.NOT_CONFIGURED,
    "602": ProviderErrorCode.NOT_CONFIGURED,
}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if key.lower() in {"token", "authorization", "api_key"} else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class JustOneProvider(ProductProvider):
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._owned_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=self.settings.justone_base_url,
            timeout=self.settings.product_provider_timeout_seconds,
        )
        self.semaphore = semaphore or asyncio.Semaphore(self.settings.provider_max_concurrency)
        self.platform_locks = {platform: asyncio.Lock() for platform in ENDPOINTS}

    async def aclose(self) -> None:
        if self._owned_client:
            await self.client.aclose()

    async def search(self, request: ProviderSearchRequest) -> ProviderPage:
        token = self.settings.justone_token
        if token is None:
            return ProviderPage(
                status=ProviderErrorCode.NOT_CONFIGURED,
                platform=request.platform,
                message="商品 Provider 尚未配置",
            )
        params: dict[str, Any] = {
            "token": token.get_secret_value(),
            "keyword": request.keyword,
        }
        if request.platform != "douyin" or request.cursor.page > 1:
            params["page"] = request.cursor.page
        if request.platform == "douyin" and request.cursor.search_id:
            params["searchId"] = request.cursor.search_id
        if request.filters.min_price is not None:
            params["startPrice"] = request.filters.min_price
        if request.filters.max_price is not None:
            params["endPrice"] = request.filters.max_price
        started = time.perf_counter()
        try:
            async with self.semaphore, self.platform_locks[request.platform]:
                response = await self.client.get(ENDPOINTS[request.platform], params=params)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("provider response is not an object")
        except (httpx.TimeoutException, httpx.NetworkError):
            return ProviderPage(
                status=ProviderErrorCode.UNKNOWN,
                platform=request.platform,
                duration_ms=int((time.perf_counter() - started) * 1000),
                message="商品数据服务响应状态未知",
            )
        except Exception:
            return ProviderPage(
                status=ProviderErrorCode.PROVIDER_ERROR,
                platform=request.platform,
                duration_ms=int((time.perf_counter() - started) * 1000),
                message="商品数据服务请求失败",
            )
        code = str(payload.get("code", payload.get("businessCode", "0")))
        items, metadata = extract_items(payload, request.platform)
        next_page = request.cursor.page + 1
        total_pages = metadata.get("total_pages")
        provider_has_more = metadata.get("has_more")
        has_more = bool(items) and (
            bool(provider_has_more)
            if provider_has_more is not None
            else total_pages is None or next_page <= total_pages
        )
        return ProviderPage(
            status=BUSINESS_STATUS.get(code, ProviderErrorCode.PROVIDER_ERROR),
            platform=request.platform,
            items=[redact(item) for item in items],
            next_cursor={
                "page": next_page,
                "search_id": metadata.get("search_id") or request.cursor.search_id,
            }
            if has_more
            else None,
            has_more=has_more,
            request_id=str(response.headers.get("x-request-id") or "") or None,
            duration_ms=int((time.perf_counter() - started) * 1000),
            business_code=code,
            message=None if code == "0" else "商品数据服务返回失败状态",
        )
