"""Bounded public catalog scopes and short-lived provider fingerprints."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from app.products.catalog.intent import ShoppingIntent
from app.products.providers.base import ProviderCursor
from app.search.schemas import Platform, SearchFilters


def _digest(value: dict) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CatalogScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_key: str
    platform: Platform
    currency: str = "CNY"
    provider: str = "justone"
    scope_version: str = "catalog-scope-v1"

    @property
    def scope_id(self) -> str:
        return f"scope_{_digest(self.model_dump(mode='json'))}"

    @classmethod
    def from_intent(
        cls, intent: ShoppingIntent, platform: Platform, *, provider: str
    ) -> CatalogScope:
        return cls(
            category_key=intent.category_key,
            platform=platform,
            currency=(intent.filters.currency or "CNY").upper(),
            provider=provider,
        )


class ProviderRequestFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_id: str
    provider: str
    platform: Platform
    normalized_query: str
    provider_filters: SearchFilters = Field(default_factory=SearchFilters)
    cursor: ProviderCursor = Field(default_factory=ProviderCursor)
    # Bump whenever request semantics or response normalization changes so a
    # previously successful-but-unusable response does not block a corrected,
    # auditable fetch forever.
    request_version: str = "provider-request-v3"

    @property
    def request_key(self) -> str:
        payload = self.model_dump(mode="json")
        payload["normalized_query"] = " ".join(self.normalized_query.lower().split())
        return _digest(payload)
