"""Strict contracts for category evidence, cards, and public insights."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type CardType = Literal["bestseller", "attribute", "price_range"]
type InsightStatus = Literal[
    "ok", "partial", "insufficient_data", "not_configured", "error"
]
EvidenceLine = Annotated[str, Field(min_length=1, max_length=80)]


class CategoryCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    card_id: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=80)
    card_type: CardType
    summary: str = Field(min_length=1, max_length=1000)
    raw_evidence: list[EvidenceLine] = Field(min_length=1, max_length=3)
    last_updated: datetime
    confidence: float = Field(ge=0, le=1)

    @field_validator("last_updated")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("last_updated 必须包含时区")
        if value.astimezone(UTC) > datetime.now(UTC):
            raise ValueError("last_updated 不能晚于当前时间")
        return value


class CategoryDocument(CategoryCard):
    model_config = ConfigDict(extra="forbid")

    category_key: str
    semantic_text: str
    content_vector: list[float]


class NormalizedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_category: str
    source_kind: str
    source_snapshot: str
    platform: Literal["taobao", "jingdong", "douyin"]
    item_id: str
    title: str
    price_cny: float = Field(gt=0)
    rating: float | None = Field(default=None, ge=0)
    sales: int | None = Field(default=None, ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime


class Bestseller(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    typical_price_cny: float = Field(ge=0)
    why_popular: str
    platform: str | None = None


class AttributeDist(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    distribution: dict[str, float]

    @model_validator(mode="after")
    def validate_distribution(self) -> AttributeDist:
        if not self.distribution:
            raise ValueError("属性分布不能为空")
        if any(value < 0 or value > 1 for value in self.distribution.values()):
            raise ValueError("属性分布值必须在 0 到 1 之间")
        if abs(sum(self.distribution.values()) - 1.0) > 0.02:
            raise ValueError("属性分布之和必须约等于 1")
        return self


class PriceTier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: Literal["budget", "mid", "premium"]
    range_cny: tuple[float, float]
    notes: str

    @model_validator(mode="after")
    def validate_range(self) -> PriceTier:
        if self.range_cny[0] < 0 or self.range_cny[0] > self.range_cny[1]:
            raise ValueError("价格档位区间无效")
        return self


class CategoryInsightOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: InsightStatus
    category: str
    depth: Literal["quick", "deep"]
    components: list[str] = Field(default_factory=list)
    bestsellers: list[Bestseller] = Field(default_factory=list)
    attributes: list[AttributeDist] = Field(default_factory=list)
    price_tiers: list[PriceTier] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    needs_external_validation: bool = True
    data_as_of: datetime | None = None
    retrieval_mode: str
    cache_hit: bool = False
    message: str | None = None


class InsightExtractionPayload(BaseModel):
    """Fields the LLM may extract; service-owned status fields are excluded."""

    model_config = ConfigDict(extra="forbid")

    components: list[str] = Field(default_factory=list)
    bestsellers: list[Bestseller] = Field(default_factory=list)
    attributes: list[AttributeDist] = Field(default_factory=list)
    price_tiers: list[PriceTier] = Field(default_factory=list)


class CategoryBuildManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_id: str
    source_path: str
    source_sha256: str
    source_rows: int = Field(ge=0)
    normalized_rows: int = Field(ge=0)
    rejected_rows: int = Field(ge=0)
    cards_generated: int = Field(ge=0)
    cards_rejected: int = Field(ge=0)
    card_counts: dict[str, int]
    rejection_reasons: dict[str, int]
    extractor: str
    prompt_version: str
    embedding_model: str
    embedding_revision: str
    embedding_dimensions: int
    semantic_text_version: str
    started_at: datetime
    finished_at: datetime
    index_name: str | None = None
