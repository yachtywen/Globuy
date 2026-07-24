"""Public ItemSearch contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

type Platform = Literal["taobao", "jingdong", "douyin"]
type Scalar = str | int | float | bool


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_price: float | None = Field(default=None, ge=0)
    max_price: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=1, max_length=8)
    min_rating: float | None = Field(default=None, ge=0)
    min_sales: int | None = Field(default=None, ge=0)
    attribute_equals: dict[str, Scalar] | None = None

    @model_validator(mode="after")
    def validate_price_range(self) -> SearchFilters:
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price 不能大于 max_price")
        return self


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    product_id: str | None = None
    offer_id: str | None = None
    platform: Platform
    title: str
    price: float
    currency: str
    rating: float | None = None
    sales: int | None = None
    image_url: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    product_url: str | None = None
    shipping_fee: float | None = Field(default=None, ge=0)
    retrieval_rank: int = Field(ge=1)
    source_kind: str = "offline_snapshot"
    data_as_of: str | None = None


class ItemSearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "not_configured", "error"]
    platform: Platform
    candidates: list[Candidate] = Field(default_factory=list)
    total_recall: int = Field(default=0, ge=0)
    truncated: bool = False
    message: str | None = None
    cache_hit: bool = False
    data_as_of: str | None = None
    source_kind: str = "offline_snapshot"
