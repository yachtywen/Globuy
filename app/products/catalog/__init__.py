"""Catalog coverage and on-demand hydration domain."""

from app.products.catalog.intent import ProductIdentity, ShoppingIntent
from app.products.catalog.scope import CatalogScope, ProviderRequestFingerprint

__all__ = ["CatalogScope", "ProductIdentity", "ProviderRequestFingerprint", "ShoppingIntent"]
