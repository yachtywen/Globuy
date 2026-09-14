"""Hard filtering, conservative grouping and one-shot LLM product reranking."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import unicodedata
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import urlparse

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.agent.llm import model_request_kwargs
from app.api.monitor import current_monitor
from app.config import get_settings
from app.database.session import Database
from app.products.catalog.intent import ProductIdentity, ShoppingIntent
from app.products.catalog.repository import CatalogRepository
from app.products.grouping import CandidateGroup, cap_groups_balanced, group_candidates
from app.recall.transient_hybrid import (
    FaissSelection,
    select_bm25_groups,
    select_faiss_groups,
)
from app.search.candidate_encoder import (
    CandidateEmbeddingEncoder,
    get_candidate_embedding_encoder,
)
from app.search.schemas import Candidate, Platform, Scalar
from app.utils.thread_ctx import current_thread_id

RANKING_VERSION = "faiss-llm-rerank-v1"


class PickerCandidate(BaseModel):
    # LLM 常把 item_search 返回的富字段（product_id/offer_id/shop_name 等）原样回传；
    # 核心字段仍严格校验，未知字段直接忽略，避免整次调用因 extra=forbid 报错。
    model_config = ConfigDict(extra="ignore")
    item_id: str
    product_id: str | None = None
    offer_id: str | None = None
    platform: Platform
    title: str
    price: float
    currency: str = "CNY"
    rating: float | None = None
    sales: int | None = None
    image_url: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    product_url: str | None = None
    shipping_fee: float | None = Field(default=None, ge=0)
    total_cost: float | None = Field(default=None, ge=0)
    retrieval_rank: int | None = None
    source_rank: int | None = None
    captured_at: str | None = None
    evidence_completeness: float = Field(default=0.0, ge=0, le=1)


class PickerConstraints(BaseModel):
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


class PickedItem(PickerCandidate):
    model_config = ConfigDict(extra="forbid")
    product_group_id: str | None = None
    alternative_offers: list[PickerCandidate] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list, max_length=3)
    flags: list[str] = Field(default_factory=list)

    @field_validator("reasons", mode="before")
    @classmethod
    def keep_top_reasons(cls, value: Any) -> Any:
        return value[:3] if isinstance(value, list) else value


class ItemPickerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "degraded", "insufficient_data"]
    picks: list[PickedItem] = Field(default_factory=list, max_length=3)
    rejected_brief: list[str] = Field(default_factory=list, max_length=8)
    ranking_method: Literal["llm", "deterministic_exact", "deterministic_fallback"]
    ranking_version: str = RANKING_VERSION
    fallback_reason: str | None = None
    duplicate_summary: dict[str, int] = Field(default_factory=dict)
    selection_rule: str = "source/retrieval rank, normalized rating, price, input order"
    candidate_selection_method: Literal[
        "not_needed", "faiss_rrf", "bm25_fallback", "deterministic_exact"
    ] = "not_needed"
    candidate_groups_before_selection: int = 0
    candidate_groups_after_selection: int = 0
    embedding_model: str | None = None
    embedding_revision: str | None = None
    embedding_cache_hits: int = 0
    embedding_cache_misses: int = 0
    embedding_duration_ms: int = 0
    bm25_duration_ms: int = 0
    faiss_duration_ms: int = 0


class RerankAssessment(BaseModel):
    # 模型输出只做引导：多余字段忽略、枚举在 _coerce_assessment 里归一化。
    model_config = ConfigDict(extra="ignore")
    product_group_id: str
    relevance: Literal["exact", "high", "medium", "low"]
    preference_fit: Literal["strong", "partial", "unknown"]
    specification_fit: Literal["strong", "partial", "unknown"]
    value: Literal["strong", "fair", "weak", "unknown"]
    evidence_quality: Literal["high", "medium", "low"]
    confidence: Literal["high", "medium", "low"]
    evidence_fields: list[str] = Field(default_factory=list, max_length=8)
    risk_codes: list[
        Literal[
            "missing_rating",
            "missing_sales",
            "missing_model",
            "stale_candidate",
            "possible_duplicate",
            "weak_preference_evidence",
        ]
    ] = Field(default_factory=list)


class RerankDecision(BaseModel):
    # 模型输出只做引导：多余字段忽略，非列表字段由宽松清洗保证。
    model_config = ConfigDict(extra="ignore")
    ordered_group_ids: list[str] = Field(min_length=1, max_length=36)
    assessments: list[RerankAssessment] = Field(default_factory=list, max_length=36)


def _normalized(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True).casefold()
    return str(value).strip().casefold()


def _parse_picker_items(raw_items: list[Any]) -> tuple[list[PickerCandidate], int]:
    """Tolerantly parse LLM-provided items; drop malformed entries instead of
    failing the whole tool call (the model echoes provider-rich candidate JSON)."""
    parsed: list[PickerCandidate] = []
    dropped = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        try:
            parsed.append(PickerCandidate.model_validate(raw))
        except ValidationError:
            dropped += 1
    return parsed, dropped


def _parse_constraints(raw: Any) -> PickerConstraints:
    if isinstance(raw, dict):
        try:
            return PickerConstraints.model_validate(raw)
        except ValidationError:
            return PickerConstraints()
    return PickerConstraints()


def _parse_intent(raw: Any) -> ShoppingIntent | None:
    if isinstance(raw, dict):
        try:
            return ShoppingIntent.model_validate(raw)
        except ValidationError:
            return None
    return None


def _hard_failure(
    item: PickerCandidate,
    constraints: PickerConstraints,
    *,
    require_source_url: bool = True,
) -> str | None:
    if not item.item_id.strip():
        return "缺少平台商品 ID"
    if not item.title.strip():
        return "缺少商品标题"
    if not math.isfinite(item.price) or item.price <= 0:
        return "商品价格非法"
    if item.currency.upper() != "CNY":
        return "币种不受支持"
    if require_source_url and (
        not item.product_url or not item.product_url.startswith(("http://", "https://"))
    ):
        return "缺少可核验来源链接"
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


def _rating_signal(value: float | None) -> float:
    if value is None:
        return -1.0
    if value <= 1:
        return value
    if value <= 5:
        return value / 5
    if value <= 100:
        return value / 100
    return -1.0


def _group_rank(group: CandidateGroup) -> tuple[float, float, float, int]:
    item = group.representative
    best_source_rank = min(
        (
            offer.source_rank or offer.retrieval_rank
            for offer in group.offers
            if offer.source_rank is not None or offer.retrieval_rank is not None
        ),
        default=10**9,
    )
    best_rating = max((_rating_signal(offer.rating) for offer in group.offers), default=-1.0)
    return (
        float(best_source_rank),
        -best_rating,
        item.price,
        group.input_order,
    )


def _as_candidate(item: PickerCandidate) -> Candidate:
    return Candidate.model_validate(item.model_dump(exclude={"total_cost"}))


def _reason_lines(item: PickerCandidate, assessment: RerankAssessment | None) -> list[str]:
    reasons: list[str] = []
    if assessment is not None:
        if assessment.relevance in {"exact", "high"}:
            reasons.append("与目标品类和用途高度相关")
        if assessment.preference_fit == "strong":
            reasons.append("较好匹配本轮软偏好")
        if assessment.value == "strong":
            reasons.append("在可比候选中价格表现较好")
        if assessment.evidence_quality == "high":
            reasons.append("商品证据字段相对完整")
    if not reasons:
        rank = item.source_rank or item.retrieval_rank
        if rank:
            reasons.append(f"来源候选顺位 {rank}")
    return list(dict.fromkeys(reasons))[:3]


def _picked(
    group: CandidateGroup,
    *,
    assessment: RerankAssessment | None,
) -> PickedItem:
    representative = PickerCandidate.model_validate(group.representative.model_dump())
    flags = list(assessment.risk_codes) if assessment else []
    if group.possible_duplicate_group_ids:
        flags.append("possible_duplicate")
    return PickedItem(
        **representative.model_dump(),
        product_group_id=group.product_group_id,
        alternative_offers=[
            PickerCandidate.model_validate(offer.model_dump()) for offer in group.offers[1:]
        ],
        reasons=_reason_lines(representative, assessment),
        flags=list(dict.fromkeys(flags)),
    )


def _prepare_groups(
    items: list[PickerCandidate],
    constraints: PickerConstraints,
    group_limit: int | None,
    *,
    require_source_url: bool = True,
) -> tuple[list[CandidateGroup], list[str], dict[str, int]]:
    accepted: list[Candidate] = []
    rejected: list[str] = []
    seen_offer_ids: set[str] = set()
    hard_filtered = 0
    same_platform_duplicates = 0
    for raw in items:
        candidate = raw if isinstance(raw, PickerCandidate) else PickerCandidate.model_validate(raw)
        failure = _hard_failure(candidate, constraints, require_source_url=require_source_url)
        identity = candidate.offer_id or candidate.item_id
        if failure is not None:
            hard_filtered += 1
            if len(rejected) < 8:
                rejected.append(f"{candidate.item_id}: {failure}")
            continue
        if identity in seen_offer_ids:
            same_platform_duplicates += 1
            continue
        seen_offer_ids.add(identity)
        accepted.append(_as_candidate(candidate))
    groups, summary = group_candidates(accepted)
    if group_limit is not None:
        groups = cap_groups_balanced(groups, group_limit)
    statistics = summary.model_dump(mode="json")
    statistics.update(
        {
            "received_offers": len(items),
            "hard_filtered_offers": hard_filtered,
            "same_platform_duplicates": same_platform_duplicates,
            "ranked_product_groups": len(groups),
        }
    )
    return groups, rejected, statistics


def _selection_fields(
    selection: FaissSelection | None,
    *,
    before: int,
    after: int,
    method: str = "not_needed",
) -> dict[str, Any]:
    return {
        "candidate_selection_method": selection.method if selection else method,
        "candidate_groups_before_selection": before,
        "candidate_groups_after_selection": after,
        "embedding_model": selection.embedding_model if selection else None,
        "embedding_revision": selection.embedding_revision if selection else None,
        "embedding_cache_hits": selection.embedding_cache_hits if selection else 0,
        "embedding_cache_misses": selection.embedding_cache_misses if selection else 0,
        "embedding_duration_ms": selection.embedding_duration_ms if selection else 0,
        "bm25_duration_ms": selection.bm25_duration_ms if selection else 0,
        "faiss_duration_ms": selection.faiss_duration_ms if selection else 0,
    }


def _identity_search_text(candidate: Candidate) -> str:
    return _identity_normalized({"title": candidate.title, "attributes": candidate.attributes})


def _identity_normalized(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _normalized(value))
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def _matches_requested_identity(candidate: Candidate, identity: ProductIdentity) -> bool:
    if identity.source_item_id:
        requested = _identity_normalized(identity.source_item_id)
        if requested not in {
            _identity_normalized(candidate.item_id),
            _identity_normalized(candidate.product_id or ""),
            _identity_normalized(candidate.offer_id or ""),
        }:
            return False
    searchable = _identity_search_text(candidate)
    if identity.model and _identity_normalized(identity.model) not in searchable:
        return False
    if identity.brand and _identity_normalized(identity.brand) not in searchable:
        return False
    return all(
        _identity_normalized(value) in searchable for value in identity.variant_attributes.values()
    )


def _requested_identity_group(
    groups: list[CandidateGroup], identity: ProductIdentity
) -> CandidateGroup | None:
    offers = [
        offer
        for group in groups
        for offer in group.offers
        if _matches_requested_identity(offer, identity)
    ]
    if not offers:
        return None
    offers.sort(
        key=lambda offer: (
            offer.price,
            offer.source_rank or offer.retrieval_rank or 10**9,
            offer.platform,
            offer.item_id,
        )
    )
    evidence = identity.model_dump(mode="json", exclude_none=True)
    digest = hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return CandidateGroup(
        product_group_id=f"pg_exact_{digest[:24]}",
        match_method="requested_identity_exact",
        identity_evidence=evidence,
        representative=offers[0],
        offers=offers,
        input_order=min(group.input_order for group in groups),
    )


def _deterministic_from_groups(
    groups: list[CandidateGroup],
    rejected: list[str],
    summary: dict[str, int],
    limit: int,
    *,
    status: Literal["ok", "degraded"] = "ok",
    ranking_method: Literal["deterministic_exact", "deterministic_fallback"] = (
        "deterministic_fallback"
    ),
    fallback_reason: str | None = None,
    selection: FaissSelection | None = None,
    before: int | None = None,
    selection_method: str = "not_needed",
) -> ItemPickerOutput:
    picks = [
        _picked(group, assessment=None)
        for group in sorted(groups, key=_group_rank)[:limit]
    ]
    return ItemPickerOutput(
        status=status if picks else "insufficient_data",
        picks=picks,
        rejected_brief=rejected,
        ranking_method=ranking_method,
        fallback_reason=fallback_reason,
        duplicate_summary=summary,
        **_selection_fields(
            selection,
            before=before if before is not None else len(groups),
            after=len(groups),
            method=selection_method,
        ),
    )


def _deterministic_output(
    items: list[PickerCandidate],
    constraints: PickerConstraints,
    limit: int,
    *,
    status: Literal["ok", "degraded"] = "ok",
    fallback_reason: str | None = None,
    require_source_url: bool = True,
) -> ItemPickerOutput:
    groups, rejected, summary = _prepare_groups(
        items,
        constraints,
        get_settings().item_rerank_group_limit,
        require_source_url=require_source_url,
    )
    return _deterministic_from_groups(
        groups,
        rejected,
        summary,
        limit,
        status=status,
        fallback_reason=fallback_reason,
    )


@tool
def item_picker(
    items: list[PickerCandidate],
    constraints: PickerConstraints | None = None,
    limit: int = 3,
    goal: str = "商品推荐",
    soft_preferences: list[str] | None = None,
) -> dict:
    """Compatibility deterministic picker used by local and legacy callers."""
    del goal, soft_preferences
    return _deterministic_output(
        items,
        constraints or PickerConstraints(),
        max(1, min(limit, 3)),
        require_source_url=False,
    ).model_dump(mode="json")


def _portable_structured_model(model: BaseChatModel) -> BaseChatModel:
    active_model = model
    if isinstance(model, ChatOpenAI):
        root_client = model.root_client.with_options(max_retries=0)
        root_async_client = model.root_async_client.with_options(max_retries=0)
        active_model = model.model_copy(
            update={
                "max_retries": 0,
                "root_client": root_client,
                "root_async_client": root_async_client,
                "client": root_client.chat.completions,
                "async_client": root_async_client.chat.completions,
            }
        )
    hostname = (
        urlparse(str(getattr(active_model, "openai_api_base", "") or "")).hostname or ""
    ).lower()
    if hostname == "api.moonshot.cn" or hostname.endswith(".api.moonshot.cn"):
        extra_body = dict(getattr(active_model, "extra_body", None) or {})
        extra_body["thinking"] = {"type": "disabled"}
        return active_model.model_copy(update={"extra_body": extra_body})
    return active_model


def _rerank_payload(groups: list[CandidateGroup]) -> list[dict[str, Any]]:
    return [
        {
            "product_group_id": group.product_group_id,
            "title": group.representative.title,
            "price": group.representative.price,
            "platforms": [offer.platform for offer in group.offers],
            "source_rank": group.representative.source_rank,
            "rating": group.representative.rating,
            "sales": group.representative.sales,
            "attributes": group.representative.attributes,
            "evidence_completeness": group.representative.evidence_completeness,
            "possible_duplicate": bool(group.possible_duplicate_group_ids),
        }
        for group in groups
    ]


_RERANK_ENUM_VALUES: dict[str, frozenset[str]] = {
    "relevance": frozenset({"exact", "high", "medium", "low"}),
    "preference_fit": frozenset({"strong", "partial", "unknown"}),
    "specification_fit": frozenset({"strong", "partial", "unknown"}),
    "value": frozenset({"strong", "fair", "weak", "unknown"}),
    "evidence_quality": frozenset({"high", "medium", "low"}),
    "confidence": frozenset({"high", "medium", "low"}),
}
_RERANK_ENUM_FALLBACK: dict[str, str] = {
    "relevance": "medium",
    "preference_fit": "unknown",
    "specification_fit": "unknown",
    "value": "unknown",
    "evidence_quality": "low",
    "confidence": "medium",
}
_RERANK_RISK_CODES = frozenset(
    {
        "missing_rating",
        "missing_sales",
        "missing_model",
        "stale_candidate",
        "possible_duplicate",
        "weak_preference_evidence",
    }
)
_RERANK_EVIDENCE_FIELDS = frozenset(
    {
        "title",
        "price",
        "platforms",
        "source_rank",
        "rating",
        "sales",
        "attributes",
        "evidence_completeness",
        "possible_duplicate",
    }
)


def _coerce_enum(value: Any, field: str) -> str:
    allowed = _RERANK_ENUM_VALUES[field]
    fallback = _RERANK_ENUM_FALLBACK[field]
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in allowed:
            return candidate
        for token in allowed:
            if token in candidate or candidate in token:
                return token
    return fallback


def _coerce_assessment(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    group_id = raw.get("product_group_id")
    if not isinstance(group_id, str) or not group_id.strip():
        return None
    return {
        "product_group_id": group_id,
        "relevance": _coerce_enum(raw.get("relevance"), "relevance"),
        "preference_fit": _coerce_enum(raw.get("preference_fit"), "preference_fit"),
        "specification_fit": _coerce_enum(raw.get("specification_fit"), "specification_fit"),
        "value": _coerce_enum(raw.get("value"), "value"),
        "evidence_quality": _coerce_enum(raw.get("evidence_quality"), "evidence_quality"),
        "confidence": _coerce_enum(raw.get("confidence"), "confidence"),
        "evidence_fields": [
            str(item)
            for item in (raw.get("evidence_fields") or [])
            if isinstance(item, (str, int)) and str(item) in _RERANK_EVIDENCE_FIELDS
        ][:8],
        "risk_codes": [
            str(item)
            for item in (raw.get("risk_codes") or [])
            if isinstance(item, str) and item in _RERANK_RISK_CODES
        ],
    }


def _coerce_decision(raw: Any) -> RerankDecision | None:
    """Lenient parse of LLM rerank output into a valid RerankDecision.

    Unknown keys are dropped, enum values normalized, assessments backfilled with
    neutral defaults, and ordered ids deduplicated/bounded. Returns None only when
    the output contains no usable ordering at all.
    """
    if not isinstance(raw, dict):
        return None
    ordered_raw = raw.get("ordered_group_ids")
    ordered: list[str] = []
    if isinstance(ordered_raw, list):
        for item in ordered_raw:
            if isinstance(item, str) and item.strip() and item not in ordered:
                ordered.append(item)
    ordered = ordered[:36]
    if not ordered:
        return None
    assessments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in (raw.get("assessments") or []):
        coerced = _coerce_assessment(item)
        if coerced is not None and coerced["product_group_id"] not in seen:
            seen.add(coerced["product_group_id"])
            assessments.append(coerced)
        if len(assessments) >= 36:
            break
    try:
        return RerankDecision.model_validate(
            {"ordered_group_ids": ordered, "assessments": assessments}
        )
    except ValidationError:
        return None


async def _lenient_rerank_decision(
    model: BaseChatModel,
    messages: list[BaseMessage],
    model_config: dict[str, Any],
) -> RerankDecision | None:
    """One plain-JSON retry when the strict function-calling parse failed."""
    try:
        reminder = SystemMessage(
            content=(
                "只输出一个 JSON 对象，不要 Markdown 代码块或注释。"
                "ordered_group_ids 必须从候选 product_group_id 中按优先级选出；"
                "assessments 的 relevance 只能取值 exact/high/medium/low，"
                "preference_fit/specification_fit/value 的 unknown 以外取值见字段说明。"
            )
        )
        reply = await model.ainvoke(
            [messages[0], reminder, *messages[1:]],
            config=model_config,
        )
        content = reply.content
        if not isinstance(content, str):
            content = str(content)
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            return None
        return _coerce_decision(json.loads(content[start : end + 1]))
    except Exception:
        return None


def _validate_decision(
    decision: RerankDecision, groups: list[CandidateGroup]
) -> tuple[list[CandidateGroup], dict[str, RerankAssessment]]:
    """Sanitize an LLM rerank decision against the real group set instead of
    rejecting it: drop duplicate/unknown ids and keep usable assessments."""
    by_id = {group.product_group_id: group for group in groups}
    ordered: list[CandidateGroup] = []
    for group_id in decision.ordered_group_ids:
        if group_id in by_id and group_id not in {item.product_group_id for item in ordered}:
            ordered.append(by_id[group_id])
    assessments: dict[str, RerankAssessment] = {}
    for item in decision.assessments:
        if item.product_group_id in by_id and item.product_group_id not in assessments:
            assessments[item.product_group_id] = item
    return ordered, assessments


@lru_cache(maxsize=1)
def _group_repository() -> CatalogRepository | None:
    settings = get_settings()
    if settings.database_url is None:
        return None
    database = Database(
        settings.database_url.get_secret_value(),
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        pool_recycle=settings.database_pool_recycle_seconds,
    )
    return CatalogRepository(database, scope_ttl_seconds=settings.catalog_scope_ttl_seconds)


def build_item_picker_tool(
    model: BaseChatModel | None,
    candidate_encoder: CandidateEmbeddingEncoder | None = None,
) -> BaseTool:
    @tool("item_picker")
    async def llm_item_picker(
        items: list[dict[str, Any]],
        config: RunnableConfig,
        constraints: dict[str, Any] | None = None,
        limit: int = 3,
        goal: str = "商品推荐",
        soft_preferences: list[str] | None = None,
        shopping_intent: dict[str, Any] | None = None,
    ) -> dict:
        """Filter, group and globally rerank provider candidates once."""
        settings = get_settings()
        # LLM-facing args are deliberately tolerant (mirrors planner): unknown fields
        # or malformed entries must not crash the whole call. Drop bad candidates,
        # ignore unusable constraints/intent, and degrade to the honest empty path.
        items, dropped_candidates = _parse_picker_items(items or [])
        constraints = _parse_constraints(constraints)
        shopping_intent = _parse_intent(shopping_intent)
        if os.environ.get("PROBE_DEBUG_ITEMS") == "1":
            for index, probe_item in enumerate(items[:50]):
                reason = _hard_failure(probe_item, constraints or PickerConstraints())
                print(
                    f"[debug-picker] #{index} id={probe_item.item_id!r} "
                    f"plat={probe_item.platform!r} url={(probe_item.product_url or '')[:36]!r} "
                    f"cur={probe_item.currency!r} price={probe_item.price!r} fail={reason}",
                    flush=True,
                )
        bounded_limit = max(1, min(limit, 3))
        active_constraints = constraints or PickerConstraints()
        # Free-text hard constraints (e.g. 降噪功能) are often echoed by the model
        # as structured required_attributes keys ({"降噪": "是"}) that no provider
        # attribute actually carries. A required key that exists in NO candidate
        # cannot discriminate anything; enforcing it only zeroes honest candidates.
        # Relax only such globally-unverifiable keys (log them); keys that exist in
        # at least one candidate keep their strict per-item evidence check.
        relaxed_required: list[str] = []
        if (
            active_constraints.required_attributes
            and items
            and len(items) >= 2
        ):
            available_attr_keys: set[str] = set()
            for candidate in items:
                available_attr_keys.update(candidate.attributes)
            relaxed_required = sorted(
                key
                for key in active_constraints.required_attributes
                if key not in available_attr_keys
            )
            if relaxed_required:
                active_constraints = active_constraints.model_copy(
                    update={
                        "required_attributes": {
                            key: value
                            for key, value in active_constraints.required_attributes.items()
                            if key not in relaxed_required
                        }
                    }
                )
        groups, rejected, summary = _prepare_groups(items, active_constraints, None)
        if relaxed_required:
            note = (
                "已放宽候选属性中不存在的必需字段："
                + "、".join(relaxed_required)
                + "（检索词已覆盖该语义，结构化校验未被放松）"
            )
            rejected = [note, *(rejected or [])]
        groups_before_selection = len(groups)
        monitor = current_monitor()
        if monitor is not None:
            await monitor.report_catalog(
                "candidate_filter_completed",
                phase="filtering",
                status="finished",
                received=len(items),
                accepted=summary["input_offers"],
                rejected=summary["hard_filtered_offers"],
                dropped_candidates=dropped_candidates,
            )
            await monitor.report_catalog(
                "candidate_grouping_completed",
                phase="grouping",
                status="finished",
                candidate_pool=summary["input_offers"],
                product_groups=summary["product_groups"],
                collapsed_offers=summary["collapsed_offers"],
                possible_duplicates=summary["possible_duplicate_pairs"],
            )
            await monitor.report_catalog(
                "shopping_intent_routed",
                phase="intent",
                status="finished",
                intent_mode=(
                    shopping_intent.intent_mode if shopping_intent else "category_explore"
                ),
                candidate_pool=len(groups),
            )
        if not groups:
            return ItemPickerOutput(
                status="insufficient_data",
                picks=[],
                rejected_brief=rejected,
                ranking_method="deterministic_fallback",
                duplicate_summary=summary,
                candidate_groups_before_selection=0,
                candidate_groups_after_selection=0,
            ).model_dump(mode="json")

        if shopping_intent and shopping_intent.intent_mode == "goal_explore":
            if monitor is not None:
                await monitor.report_catalog(
                    "intent_clarification_requested",
                    phase="intent",
                    status="blocked",
                    clarification_count=shopping_intent.clarification_count,
                )
            return ItemPickerOutput(
                status="insufficient_data",
                picks=[],
                rejected_brief=rejected,
                ranking_method="deterministic_fallback",
                fallback_reason="insufficient_intent",
                duplicate_summary=summary,
                candidate_groups_before_selection=len(groups),
                candidate_groups_after_selection=0,
            ).model_dump(mode="json")

        if shopping_intent and shopping_intent.intent_mode == "exact_product":
            identity = shopping_intent.product_identity
            if identity is None:  # Defensive guard for deserialized legacy checkpoints.
                raise ValueError("exact_product requires product_identity")
            exact_group = _requested_identity_group(groups, identity)
            exact_groups = [exact_group] if exact_group is not None else []
            output = _deterministic_from_groups(
                exact_groups,
                rejected,
                summary,
                1,
                ranking_method="deterministic_exact",
                fallback_reason=None if exact_groups else "exact_identity_not_found",
                before=groups_before_selection,
                selection_method="deterministic_exact",
            )
            return output.model_dump(mode="json")

        selection: FaissSelection | None = None
        if shopping_intent is not None and groups:
            if monitor is not None:
                await monitor.report_catalog(
                    "candidate_faiss_started",
                    phase="candidate_selection",
                    status="running",
                    candidate_pool=len(groups),
                    candidate_limit=settings.candidate_faiss_group_limit,
                )
            faiss_started = time.perf_counter()
            try:
                active_encoder = candidate_encoder or get_candidate_embedding_encoder()
                async with asyncio.timeout(settings.candidate_embedding_timeout_seconds):
                    selection = await asyncio.to_thread(
                        select_faiss_groups,
                        groups,
                        lexical_query=shopping_intent.lexical_query
                        or shopping_intent.primary_query
                        or goal,
                        semantic_query=shopping_intent.semantic_query
                        or shopping_intent.primary_query
                        or goal,
                        encoder=active_encoder,
                        limit=settings.candidate_faiss_group_limit,
                        rank_constant=settings.candidate_rrf_rank_constant,
                    )
                groups = selection.groups
                if monitor is not None:
                    await monitor.report_catalog(
                        "candidate_faiss_completed",
                        phase="candidate_selection",
                        status="finished",
                        candidate_pool=groups_before_selection,
                        returned=len(groups),
                        duration_ms=int((time.perf_counter() - faiss_started) * 1000),
                        embedding_duration_ms=selection.embedding_duration_ms,
                        bm25_duration_ms=selection.bm25_duration_ms,
                        faiss_duration_ms=selection.faiss_duration_ms,
                        embedding_cache_hits=selection.embedding_cache_hits,
                        embedding_cache_misses=selection.embedding_cache_misses,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                selection = select_bm25_groups(
                    groups,
                    shopping_intent.lexical_query or shopping_intent.primary_query or goal,
                    settings.candidate_faiss_group_limit,
                )
                groups = selection.groups
                if monitor is not None:
                    await monitor.report_catalog(
                        "candidate_faiss_degraded",
                        phase="candidate_selection",
                        status="degraded",
                        candidate_pool=groups_before_selection,
                        returned=len(groups),
                        duration_ms=int((time.perf_counter() - faiss_started) * 1000),
                        fallback_reason=type(exc).__name__.casefold(),
                    )
        elif len(groups) > settings.candidate_faiss_group_limit:
            groups = cap_groups_balanced(groups, settings.candidate_faiss_group_limit)

        summary["ranked_product_groups"] = len(groups)
        repository = _group_repository()
        if repository is not None:
            try:
                await repository.persist_product_groups(groups)
            except Exception:
                pass
        if model is None:
            output = _deterministic_from_groups(
                groups,
                rejected,
                summary,
                bounded_limit,
                status="degraded",
                fallback_reason="reranker_not_configured",
                selection=selection,
                before=groups_before_selection,
            )
            if monitor is not None:
                await monitor.report_catalog(
                    "llm_rerank_degraded",
                    phase="reranking",
                    status="degraded",
                    candidate_pool=len(groups),
                    ranking_method="deterministic_fallback",
                )
            return output.model_dump(mode="json")
        if monitor is not None:
            await monitor.report_catalog(
                "llm_rerank_started",
                phase="reranking",
                status="running",
                candidate_pool=len(groups),
                ranking_method="llm",
            )
        started = time.perf_counter()
        try:
            structured = _portable_structured_model(model).with_structured_output(
                RerankDecision, method="function_calling"
            )
            messages = [
                SystemMessage(
                    content=(
                        "你是商品候选精排器。只能排序输入的 product_group_id，"
                        "不得修改或补充事实。硬约束已经由代码执行，不得让被过滤商品"
                        "重新出现。综合考察查询相关性、软偏好、规格、同规格性价比、"
                        "证据质量、新鲜度和 Top3 差异化。评分和销量缺失不等于质量差，"
                        "不同平台量纲不得直接比较。"
                    )
                ),
                HumanMessage(
                    content=json.dumps(
                        {
                            "goal": goal,
                            "soft_preferences": soft_preferences or [],
                            "candidates": _rerank_payload(groups),
                            "output_limit": bounded_limit,
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
            model_config = dict(config or {})
            model_config["run_name"] = "item_picker.rerank"
            model_config["tags"] = [*model_config.get("tags", []), "item_rerank"]
            async with asyncio.timeout(settings.item_rerank_timeout_seconds):
                decision: RerankDecision | None = None
                try:
                    response = await structured.ainvoke(
                        messages,
                        config=model_config,
                        **model_request_kwargs(current_thread_id(), model=model),
                    )
                    decision = (
                        response
                        if isinstance(response, RerankDecision)
                        else RerankDecision.model_validate(response)
                    )
                except Exception:
                    decision = None
                if decision is None:
                    decision = await _lenient_rerank_decision(model, messages, model_config)
            if decision is None:
                raise RuntimeError("LLM rerank output unusable after lenient retry")
            ordered, assessments = _validate_decision(decision, groups)
            if not ordered:
                raise RuntimeError("LLM rerank returned no usable ordering")
            seen = {group.product_group_id for group in ordered}
            ordered.extend(
                group
                for group in sorted(groups, key=_group_rank)
                if group.product_group_id not in seen
            )
            picks = [
                _picked(
                    group,
                    assessment=assessments.get(group.product_group_id),
                )
                for group in ordered[:bounded_limit]
            ]
            if monitor is not None:
                await monitor.report_catalog(
                    "llm_rerank_completed",
                    phase="reranking",
                    status="finished",
                    candidate_pool=len(groups),
                    returned=len(picks),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    ranking_method="llm",
                )
            return ItemPickerOutput(
                status="ok",
                picks=picks,
                rejected_brief=rejected,
                ranking_method="llm",
                duplicate_summary=summary,
                **_selection_fields(
                    selection,
                    before=groups_before_selection,
                    after=len(groups),
                ),
            ).model_dump(mode="json")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = type(exc).__name__.casefold()
            output = _deterministic_from_groups(
                groups,
                rejected,
                summary,
                bounded_limit,
                status="degraded",
                fallback_reason=reason,
                selection=selection,
                before=groups_before_selection,
            )
            if monitor is not None:
                await monitor.report_catalog(
                    "llm_rerank_degraded",
                    phase="reranking",
                    status="degraded",
                    candidate_pool=len(groups),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    ranking_method="deterministic_fallback",
                )
            return output.model_dump(mode="json")

    return llm_item_picker
