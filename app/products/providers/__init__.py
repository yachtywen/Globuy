"""Provider-neutral product search adapters."""

from app.products.providers.base import (
    ProductProvider,
    ProviderCursor,
    ProviderErrorCode,
    ProviderPage,
    ProviderSearchRequest,
)

__all__ = [
    "ProductProvider",
    "ProviderCursor",
    "ProviderErrorCode",
    "ProviderPage",
    "ProviderSearchRequest",
]
