"""Validated shopping intent used before any paid catalog lookup."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.search.schemas import Platform, Scalar, SearchFilters


class ProductIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=128)
    source_item_id: str | None = Field(default=None, max_length=256)
    variant_attributes: dict[str, Scalar] = Field(default_factory=dict)

    @model_validator(mode="after")
    def has_stable_identity(self) -> ProductIdentity:
        if not self.model and not self.source_item_id:
            raise ValueError("exact product identity requires model or source_item_id")
        return self


class ShoppingIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal["product_search"] = "product_search"
    intent_mode: Literal["exact_product", "category_explore", "goal_explore"] = (
        "category_explore"
    )
    intent_confidence: Literal["high", "medium", "low"] = "medium"
    product_identity: ProductIdentity | None = None
    category_key: str | None = Field(default=None, min_length=1, max_length=128)
    category_name: str | None = Field(default=None, min_length=1, max_length=128)
    primary_query: str | None = Field(default=None, min_length=1, max_length=200)
    lexical_query: str | None = Field(default=None, min_length=1, max_length=500)
    semantic_query: str | None = Field(default=None, min_length=1, max_length=1000)
    query_variants: list[str] = Field(default_factory=list)
    platforms: list[Platform] = Field(min_length=1)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    blocked_platforms: list[Platform] = Field(default_factory=list)
    blocked_item_ids: list[str] = Field(default_factory=list, max_length=100)
    required_attributes: dict[str, Scalar] = Field(default_factory=dict)
    excluded_attributes: dict[str, list[Scalar]] = Field(default_factory=dict)
    hard_constraints: list[str] = Field(default_factory=list, max_length=20)
    soft_preferences: list[str] = Field(default_factory=list, max_length=20)
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=500)
    clarification_count: int = Field(default=0, ge=0, le=2)

    @field_validator("category_key")
    @classmethod
    def normalize_category_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = "_".join(value.strip().lower().replace("-", "_").split())
        if not normalized:
            raise ValueError("category_key 不能为空")
        return normalized

    @field_validator("category_name", "primary_query", "lexical_query", "semantic_query")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("query_variants")
    @classmethod
    def normalize_variants(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in values if item.strip()))
        if len(normalized) > 2:
            raise ValueError("query_variants 最多允许 2 个")
        return normalized

    @field_validator("platforms", "blocked_platforms")
    @classmethod
    def unique_platforms(cls, values: list[Platform]) -> list[Platform]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def clarification_contract(self) -> ShoppingIntent:
        if self.needs_clarification and not self.clarification_question:
            raise ValueError("needs_clarification=true 时必须提供 clarification_question")
        if self.intent_mode == "goal_explore":
            if not self.needs_clarification:
                raise ValueError("goal_explore 必须先澄清并禁止商品检索")
            return self
        if not self.category_key or not self.category_name or not self.primary_query:
            raise ValueError("可执行商品意图必须包含品类和 primary_query")
        if self.intent_mode == "exact_product":
            if self.product_identity is None:
                raise ValueError("exact_product 必须包含 product_identity")
        else:
            # Backward-compatible defaults for existing callers and stored checkpoints.
            self.lexical_query = self.lexical_query or self.primary_query
            self.semantic_query = self.semantic_query or " ".join(
                [self.primary_query, *self.soft_preferences]
            ).strip()
        return self

    @property
    def provider_allowed(self) -> bool:
        return not self.needs_clarification
