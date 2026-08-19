"""Validated shopping intent used before any paid catalog lookup."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.search.schemas import Platform, SearchFilters


class ShoppingIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal["product_search"] = "product_search"
    category_key: str = Field(min_length=1, max_length=128)
    category_name: str = Field(min_length=1, max_length=128)
    primary_query: str = Field(min_length=1, max_length=200)
    query_variants: list[str] = Field(default_factory=list)
    platforms: list[Platform] = Field(min_length=1)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    hard_constraints: list[str] = Field(default_factory=list, max_length=20)
    soft_preferences: list[str] = Field(default_factory=list, max_length=20)
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=500)

    @field_validator("category_key")
    @classmethod
    def normalize_category_key(cls, value: str) -> str:
        normalized = "_".join(value.strip().lower().replace("-", "_").split())
        if not normalized:
            raise ValueError("category_key 不能为空")
        return normalized

    @field_validator("category_name", "primary_query")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("query_variants")
    @classmethod
    def normalize_variants(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in values if item.strip()))
        if len(normalized) > 2:
            raise ValueError("query_variants 最多允许 2 个")
        return normalized

    @field_validator("platforms")
    @classmethod
    def unique_platforms(cls, values: list[Platform]) -> list[Platform]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def clarification_contract(self) -> ShoppingIntent:
        if self.needs_clarification and not self.clarification_question:
            raise ValueError("needs_clarification=true 时必须提供 clarification_question")
        return self

    @property
    def provider_allowed(self) -> bool:
        return not self.needs_clarification
