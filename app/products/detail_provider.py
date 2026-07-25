"""Product detail provider contracts and runtime registration."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.config import Settings


@dataclass(frozen=True)
class ProductDetail:
    price: Decimal
    currency: str


class ProductProvider(Protocol):
    async def get_detail(self, platform: str, source_item_id: str) -> ProductDetail: ...


class ProviderNotConfigured(RuntimeError):
    pass


class ProviderDetailError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class ProviderRegistry:
    def __init__(self, providers: dict[str, ProductProvider] | None = None) -> None:
        self.providers = providers or {}

    async def get_detail(self, platform: str, source_item_id: str) -> ProductDetail:
        provider = self.providers.get(platform)
        if provider is None:
            raise ProviderNotConfigured(f"{platform} detail provider is not configured")
        return await provider.get_detail(platform, source_item_id)


def build_provider_registry(settings: Settings) -> ProviderRegistry:
    if settings.realtime_product_provider != "justone" or settings.justone_api_token is None:
        return ProviderRegistry()

    from app.products.justone_detail import JustOneDetailProvider

    provider = JustOneDetailProvider(settings)
    return ProviderRegistry(
        {
            "taobao": provider,
            "jingdong": provider,
        }
    )
