"""Deterministic candidate shortlisting without a learned score."""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.category.schemas import CategoryInsightOutput
from app.search.schemas import Platform, Scalar


class PickerCandidate(BaseModel):
    """One normalized candidate accepted by ItemPicker."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    product_id: str | None = None
    offer_id: str | None = None
    platform: Platform
    title: str = Field(min_length=1)
    price: float = Field(ge=0)
    currency: Literal["CNY"] = "CNY"
    rating: float | None = Field(default=None, ge=0)
    sales: int | None = Field(default=None, ge=0)
    image_url: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    product_url: str | None = None
    shipping_fee: float | None = Field(default=None, ge=0)
    total_cost: float | None = Field(default=None, ge=0)
    retrieval_rank: int | None = Field(default=None, ge=1)


class PickerConstraints(BaseModel):
    """Explicit hard constraints; absent evidence never counts as a match."""

    model_config = ConfigDict(extra="forbid")

    min_price: float | None = Field(default=None, ge=0)
    max_price: float | None = Field(default=None, ge=0)
    blocked_item_ids: list[str] = Field(default_factory=list)
    blocked_platforms: list[Platform] = Field(default_factory=list)
    required_attributes: dict[str, Scalar] = Field(default_factory=dict)
    excluded_attributes: dict[str, list[Scalar]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_price_range(self) -> PickerConstraints:
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price 不能大于 max_price")
        return self


class CategoryAnnotations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price_tier: str | None = None
    matched_typical_attributes: list[dict[str, str]] = Field(default_factory=list)
    component_coverage: list[str] = Field(default_factory=list)


class PickedItem(PickerCandidate):
    model_config = ConfigDict(extra="forbid")

    reasons: list[str] = Field(default_factory=list, max_length=3)
    flags: list[str] = Field(default_factory=list)
    category_annotations: CategoryAnnotations = Field(default_factory=CategoryAnnotations)


class ItemPickerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "insufficient_data"]
    picks: list[PickedItem] = Field(default_factory=list, max_length=3)
    rejected_brief: list[str] = Field(default_factory=list, max_length=8)
    selection_rule: str = "retrieval_rank asc, rating desc, price asc, input order"


def _normalized(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True).casefold()
    return str(value).strip().casefold()


def _hard_failure(item: PickerCandidate, constraints: PickerConstraints) -> str | None:
    if item.item_id in constraints.blocked_item_ids:
        return "命中商品黑名单"
    if item.platform in constraints.blocked_platforms:
        return "命中平台黑名单"
    if constraints.min_price is not None and item.price < constraints.min_price:
        return f"价格低于下限 {constraints.min_price:g} CNY"
    if constraints.max_price is not None and item.price > constraints.max_price:
        return f"价格超过预算 {constraints.max_price:g} CNY"

    for key, expected in constraints.required_attributes.items():
        if key not in item.attributes:
            return f"缺少硬约束属性证据：{key}"
        if _normalized(item.attributes[key]) != _normalized(expected):
            return f"属性 {key} 不满足要求"

    for key, excluded in constraints.excluded_attributes.items():
        if key not in item.attributes:
            return f"缺少黑名单属性证据：{key}"
        actual = _normalized(item.attributes[key])
        if any(_normalized(value) in actual for value in excluded):
            return f"属性 {key} 命中黑名单"
    return None


def _category_annotations(
    item: PickerCandidate, context: CategoryInsightOutput | None
) -> CategoryAnnotations:
    annotations = CategoryAnnotations()
    if context is None or context.status not in {"ok", "partial"}:
        return annotations

    for tier in context.price_tiers:
        if tier.range_cny[0] <= item.price <= tier.range_cny[1]:
            annotations.price_tier = tier.tier
            break

    searchable = f"{item.title} {_normalized(item.attributes)}".casefold()
    for attribute in context.attributes:
        typical = sorted(attribute.distribution.items(), key=lambda pair: (-pair[1], pair[0]))
        for value, _ in typical:
            if value != "unknown" and value.casefold() in searchable:
                annotations.matched_typical_attributes.append(
                    {"name": attribute.name, "value": value}
                )
                break
    annotations.component_coverage = [
        component for component in context.components if component.casefold() in searchable
    ]
    return annotations


def _rank(
    pair: tuple[int, PickerCandidate],
) -> tuple[float, float, float, int]:
    position, item = pair
    return (
        float(item.retrieval_rank) if item.retrieval_rank is not None else float("inf"),
        -float(item.rating) if item.rating is not None else 0.0,
        float(item.price),
        position,
    )


@tool
def item_picker(
    items: list[PickerCandidate],
    constraints: PickerConstraints | None = None,
    category_context: CategoryInsightOutput | None = None,
    limit: int = 3,
) -> dict:
    """Apply explicit hard constraints, then deterministic retrieval ordering."""

    bounded_limit = max(1, min(limit, 3))
    active_constraints = constraints or PickerConstraints()
    accepted: list[tuple[int, PickerCandidate]] = []
    rejected: list[str] = []
    for position, raw in enumerate(items):
        candidate = raw if isinstance(raw, PickerCandidate) else PickerCandidate.model_validate(raw)
        failure = _hard_failure(candidate, active_constraints)
        if failure is not None:
            if len(rejected) < 8:
                rejected.append(f"{candidate.item_id}: {failure}")
            continue
        accepted.append((position, candidate))

    picks: list[PickedItem] = []
    for _, candidate in sorted(accepted, key=_rank)[:bounded_limit]:
        annotations = _category_annotations(candidate, category_context)
        reasons = [f"检索顺位 {candidate.retrieval_rank}"] if candidate.retrieval_rank else []
        if annotations.price_tier:
            reasons.append(f"品类价格档位：{annotations.price_tier}")
        if annotations.matched_typical_attributes:
            reasons.append("匹配有证据的典型属性")
        picks.append(
            PickedItem(
                **candidate.model_dump(),
                reasons=reasons[:3],
                flags=[],
                category_annotations=annotations,
            )
        )

    output = ItemPickerOutput(
        status="ok" if picks else "insufficient_data",
        picks=picks,
        rejected_brief=rejected,
    )
    return output.model_dump(mode="json")
