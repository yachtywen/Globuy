"""Conservative cross-platform product identity resolution.

Only exact, auditable identifiers may collapse offers into one product group. Title
similarity is deliberately advisory so a visually similar bundle or variant never
disappears from the candidate set.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.search.schemas import Candidate, Platform

IDENTITY_VERSION = "product-identity-v1"
GTIN_KEYS = ("gtin", "ean", "upc", "barcode")
VARIANT_KEYS = (
    "capacity",
    "storage",
    "size",
    "color",
    "version",
    "edition",
    "package",
    "bundle",
    "sku",
    "mpn",
)


class CandidateGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_group_id: str
    match_method: Literal["singleton", "gtin_exact", "brand_model_variant_exact"]
    identity_evidence: dict[str, Any] = Field(default_factory=dict)
    representative: Candidate
    offers: list[Candidate] = Field(min_length=1)
    input_order: int = Field(ge=0)
    possible_duplicate_group_ids: list[str] = Field(default_factory=list)


class GroupingSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_offers: int
    product_groups: int
    collapsed_offers: int
    possible_duplicate_pairs: int


def _text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def _find_attribute(attributes: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    lowered = {str(key).casefold(): value for key, value in attributes.items()}
    for key in keys:
        value = lowered.get(key)
        if value not in (None, "", [], {}):
            result = _text(value)
            if result:
                return result
    return None


def _identity(candidate: Candidate) -> tuple[str, str, dict[str, Any]]:
    attributes = candidate.attributes
    verified_gtin = _find_attribute(attributes, ("verified_gtin",))
    gtin_is_verified = str(attributes.get("gtin_verified", "")).casefold() in {
        "true",
        "1",
        "yes",
        "verified",
    }
    gtin = verified_gtin or (_find_attribute(attributes, GTIN_KEYS) if gtin_is_verified else None)
    if gtin:
        evidence = {"gtin": gtin}
        return "gtin_exact", f"gtin:{gtin}", evidence

    brand = _find_attribute(attributes, ("brand", "品牌"))
    model = _find_attribute(attributes, ("model", "型号", "model_number"))
    variants = {
        key: normalized
        for key in VARIANT_KEYS
        if (normalized := _find_attribute(attributes, (key,)))
    }
    # Missing variant evidence is intentionally not enough for a live auto-merge.
    if brand and model and variants:
        evidence = {"brand": brand, "model": model, "variants": variants}
        canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        return "brand_model_variant_exact", canonical, evidence

    singleton = candidate.product_id or candidate.item_id
    # Product IDs are only stable inside one platform. Including the platform is
    # essential: an identical-looking opaque ID is not cross-platform evidence.
    identity_key = f"singleton:{candidate.platform}:{singleton}"
    return (
        "singleton",
        identity_key,
        {
            "platform": candidate.platform,
            "product_id": singleton,
        },
    )


def _group_id(identity_key: str) -> str:
    digest = hashlib.sha256(f"{IDENTITY_VERSION}:{identity_key}".encode()).hexdigest()
    return f"pg_{digest[:32]}"


def _offer_sort(pair: tuple[int, Candidate]) -> tuple[float, float, int]:
    position, candidate = pair
    return (
        float(candidate.price),
        float(candidate.source_rank or candidate.retrieval_rank or 10**9),
        position,
    )


def _title_tokens(title: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    return set(
        re.findall(r"[a-z]+\d+[a-z0-9-]*|\d+[a-z]+[a-z0-9-]*|[\u4e00-\u9fff]{2,}", normalized)
    )


def _title_similarity(left: str, right: str) -> float:
    a, b = _title_tokens(left), _title_tokens(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def group_candidates(items: list[Candidate]) -> tuple[list[CandidateGroup], GroupingSummary]:
    buckets: dict[str, list[tuple[int, Candidate]]] = defaultdict(list)
    identities: dict[str, tuple[str, dict[str, Any]]] = {}
    for position, raw in enumerate(items):
        candidate = raw if isinstance(raw, Candidate) else Candidate.model_validate(raw)
        method, identity_key, evidence = _identity(candidate)
        group_id = _group_id(identity_key)
        buckets[group_id].append((position, candidate))
        identities[group_id] = (method, evidence)

    groups: list[CandidateGroup] = []
    for group_id, offers in buckets.items():
        ordered = [candidate for _, candidate in sorted(offers, key=_offer_sort)]
        method, evidence = identities[group_id]
        groups.append(
            CandidateGroup(
                product_group_id=group_id,
                match_method=method,
                identity_evidence=evidence,
                representative=ordered[0],
                offers=ordered,
                input_order=min(position for position, _ in offers),
            )
        )
    groups.sort(
        key=lambda group: (
            min(
                (
                    offer.source_rank or offer.retrieval_rank
                    for offer in group.offers
                    if offer.source_rank is not None or offer.retrieval_rank is not None
                ),
                default=10**9,
            ),
            group.input_order,
        )
    )

    possible_pairs = 0
    possible: dict[str, set[str]] = defaultdict(set)
    for index, left in enumerate(groups):
        for right in groups[index + 1 :]:
            if left.match_method != "singleton" or right.match_method != "singleton":
                continue
            if _title_similarity(left.representative.title, right.representative.title) >= 0.82:
                possible[left.product_group_id].add(right.product_group_id)
                possible[right.product_group_id].add(left.product_group_id)
                possible_pairs += 1
    groups = [
        group.model_copy(
            update={"possible_duplicate_group_ids": sorted(possible[group.product_group_id])}
        )
        for group in groups
    ]
    return groups, GroupingSummary(
        input_offers=len(items),
        product_groups=len(groups),
        collapsed_offers=max(0, len(items) - len(groups)),
        possible_duplicate_pairs=possible_pairs,
    )


def cap_groups_balanced(groups: list[CandidateGroup], limit: int) -> list[CandidateGroup]:
    """Round-robin platform buckets while preserving each provider's source order."""

    buckets: dict[Platform, list[CandidateGroup]] = {
        key: [] for key in ("taobao", "jingdong", "douyin")
    }
    for group in groups:
        buckets[group.representative.platform].append(group)
    selected: list[CandidateGroup] = []
    while len(selected) < limit and any(buckets.values()):
        for platform in ("taobao", "jingdong", "douyin"):
            if buckets[platform] and len(selected) < limit:
                selected.append(buckets[platform].pop(0))
    return selected


def evidence_completeness(candidate: Candidate) -> float:
    attributes = candidate.attributes
    fields = (
        bool(candidate.title),
        bool(candidate.product_url),
        candidate.price > 0,
        bool(_find_attribute(attributes, ("brand", "品牌"))),
        bool(_find_attribute(attributes, ("model", "型号", "model_number"))),
        candidate.rating is not None,
        candidate.sales is not None,
        bool(candidate.captured_at),
    )
    return round(sum(fields) / len(fields), 3)
