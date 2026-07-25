"""Verified Just One product-price adapter for wishlist refreshes."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import Settings
from app.products.detail_provider import ProductDetail, ProviderDetailError

_ENDPOINTS = {
    "taobao": "/api/taobao/get-item-detail/v9",
    "jingdong": "/api/jd/get-item-price/v1",
}


def _path(payload: Any, *parts: str | int) -> Any:
    value = payload
    for part in parts:
        if isinstance(part, int):
            if not isinstance(value, list) or len(value) <= part:
                return None
            value = value[part]
        else:
            if not isinstance(value, dict):
                return None
            value = value.get(part)
    return value


def _decimal_price(value: Any, *, cents: bool = False) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "").replace("¥", "").replace("￥", "")
    try:
        price = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if cents:
        price /= 100
    return price.quantize(Decimal("0.01")) if price >= 0 else None


def _taobao_price(payload: dict[str, Any]) -> Decimal | None:
    paths: tuple[tuple[str | int, ...], ...] = (
        ("data", "mainItemInfo", "afterCouponAmountPrice"),
        (
            "data",
            "mainItemInfo",
            "taobaoPromotionModel",
            "finalPromotionInfo",
            "finalPromotionPrice",
        ),
        (
            "data",
            "mainItemInfo",
            "promotionModel",
            "promotionPriceModel",
            "promotionPrice",
        ),
        ("data", "price"),
        ("data", "skus", 0, "price"),
    )
    return next(
        (price for parts in paths if (price := _decimal_price(_path(payload, *parts))) is not None),
        None,
    )


def _jingdong_price(payload: dict[str, Any]) -> Decimal | None:
    return _decimal_price(_path(payload, "data", "data", 0, "price"))


_PARSERS = {
    "taobao": _taobao_price,
    "jingdong": _jingdong_price,
}


class JustOneDetailProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def get_detail(self, platform: str, source_item_id: str) -> ProductDetail:
        endpoint = _ENDPOINTS.get(platform)
        parser = _PARSERS.get(platform)
        if endpoint is None or parser is None:
            raise ProviderDetailError("provider_platform_unsupported", "unsupported platform")
        token = self.settings.justone_api_token
        if token is None:
            raise ProviderDetailError("provider_unavailable", "Just One token is not configured")
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.justone_base_url,
                timeout=self.settings.realtime_search_timeout_seconds,
                headers={"Accept": "application/json"},
                transport=self.transport,
            ) as client:
                response = await client.get(
                    endpoint,
                    params={"token": token.get_secret_value(), "itemId": source_item_id},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderDetailError(
                "provider_request_error", f"Just One detail request failed: {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderDetailError("provider_invalid_response", "invalid response payload")
        if str(payload.get("code", "unknown")) != "0":
            raise ProviderDetailError(
                "provider_business_error",
                f"Just One business error: {payload.get('code', 'unknown')}",
            )
        price = parser(payload)
        if price is None:
            raise ProviderDetailError("provider_price_missing", "verified price field is missing")
        return ProductDetail(price=price, currency="CNY")
