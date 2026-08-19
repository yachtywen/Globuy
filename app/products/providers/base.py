"""Strict contracts shared by paid and mocked product providers."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.search.schemas import Platform, SearchFilters


class ProviderErrorCode(StrEnum):
    OK = "ok"
    NOT_CONFIGURED = "not_configured"
    PROVIDER_ERROR = "provider_error"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    INVALID_REQUEST = "invalid_request"
    UNKNOWN = "unknown"


class ProviderCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1)
    search_id: str | None = Field(default=None, max_length=512)


class ProviderSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    platform: Platform
    keyword: str = Field(min_length=1, max_length=200)
    cursor: ProviderCursor = Field(default_factory=ProviderCursor)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    request_key: str = Field(min_length=32, max_length=64)


class ProviderPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ProviderErrorCode
    platform: Platform
    items: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: ProviderCursor | None = None
    has_more: bool = False
    request_id: str | None = Field(default=None, max_length=255)
    duration_ms: int = Field(default=0, ge=0)
    business_code: str | None = Field(default=None, max_length=32)
    message: str | None = Field(default=None, max_length=500)


class ProductProvider(Protocol):
    async def search(self, request: ProviderSearchRequest) -> ProviderPage: ...
