from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.products.detail_provider import ProviderDetailError, build_provider_registry
from app.products.justone_detail import JustOneDetailProvider


def _provider(payload: dict, expected_path: str) -> JustOneDetailProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == expected_path
        assert request.url.params["itemId"] == "item-1"
        assert request.url.params["token"] == "test-token"
        return httpx.Response(200, json=payload)

    settings = Settings(
        realtime_product_provider="justone",
        justone_api_token="test-token",
        justone_base_url="https://provider.test",
    )
    return JustOneDetailProvider(settings, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_taobao_detail_uses_verified_current_price_path() -> None:
    provider = _provider(
        {"code": 0, "data": {"mainItemInfo": {"afterCouponAmountPrice": "239.90"}}},
        "/api/taobao/get-item-detail/v9",
    )
    detail = await provider.get_detail("taobao", "item-1")
    assert detail.price == Decimal("239.90")
    assert detail.currency == "CNY"


@pytest.mark.asyncio
async def test_jingdong_detail_uses_verified_price_path() -> None:
    provider = _provider(
        {"code": 0, "data": {"data": [{"price": "199.5"}]}},
        "/api/jd/get-item-price/v1",
    )
    detail = await provider.get_detail("jingdong", "item-1")
    assert detail.price == Decimal("199.50")


@pytest.mark.asyncio
async def test_detail_business_error_is_structured() -> None:
    provider = _provider(
        {"code": 301, "message": "collection failed", "data": {}},
        "/api/jd/get-item-price/v1",
    )
    with pytest.raises(ProviderDetailError) as caught:
        await provider.get_detail("jingdong", "item-1")
    assert caught.value.error_code == "provider_business_error"


@pytest.mark.asyncio
async def test_detail_missing_verified_price_is_structured() -> None:
    provider = _provider(
        {"code": 0, "data": {}},
        "/api/taobao/get-item-detail/v9",
    )
    with pytest.raises(ProviderDetailError) as caught:
        await provider.get_detail("taobao", "item-1")
    assert caught.value.error_code == "provider_price_missing"


def test_registry_only_configures_platforms_with_verified_price_paths() -> None:
    settings = Settings(
        realtime_product_provider="justone",
        justone_api_token="test-token",
    )
    registry = build_provider_registry(settings)
    assert set(registry.providers) == {"taobao", "jingdong"}
