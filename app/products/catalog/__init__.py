"""Catalog coverage and on-demand hydration domain."""

from app.products.catalog.intent import ShoppingIntent
from app.products.catalog.scope import CatalogScope, ProviderRequestFingerprint

__all__ = ["CatalogScope", "ProviderRequestFingerprint", "ShoppingIntent"]
