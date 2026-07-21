"""Deterministic CategoryCard fact aggregation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from statistics import quantiles

from app.category.schemas import CategoryCard, NormalizedEvidence

ATTRIBUTE_RULES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "连接方式": (
        ("蓝牙/无线", ("蓝牙", "无线", "真无线")),
        ("有线", ("有线", "线控", "3.5mm", "type-c", "usb")),
    ),
    "佩戴形态": (
        ("开放式/骨传导", ("开放式", "骨传导", "不入耳", "耳夹")),
        ("头戴式", ("头戴", "耳罩")),
        ("入耳式/耳塞", ("入耳", "耳塞", "半入耳")),
    ),
    "降噪能力": (("明确降噪", ("主动降噪", "降噪", "anc")),),
    "使用场景": (
        ("游戏/电竞", ("游戏", "电竞", "听声辨位")),
        ("运动", ("运动", "跑步", "骑行", "游泳")),
        ("通勤/旅行", ("通勤", "旅行", "飞机", "地铁")),
    ),
}


def _stable_card_id(category: str, card_type: str, partition: str, snapshot: str) -> str:
    digest = hashlib.sha256(
        f"{category}|{card_type}|{partition}|{snapshot}".encode()
    ).hexdigest()[:16]
    return f"{category}-{card_type}-{partition}-{digest}"


def _compact(text: str, limit: int = 80) -> str:
    normalized = " ".join(text.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"


def _facts_text(item: NormalizedEvidence) -> str:
    sales = str(item.sales) if item.sales is not None else "unknown"
    rating = f"{item.rating:.4g}" if item.rating is not None else "unknown"
    return _compact(
        f"{item.platform}|{item.item_id}|销量={sales}|评分={rating}|价={item.price_cny:.2f}"
    )


def _price_card(
    items: list[NormalizedEvidence], *, min_confidence: float
) -> CategoryCard | None:
    prices = sorted(item.price_cny for item in items if item.price_cny > 0)
    confidence = len(prices) / len(items) if items else 0.0
    if len(prices) < 3 or confidence < min_confidence:
        return None
    q33, q67 = quantiles(prices, n=3, method="inclusive")
    q33, q67 = round(q33, 2), round(q67, 2)
    if q33 >= q67:
        return None
    low, high = round(prices[0], 2), round(prices[-1], 2)
    snapshot = items[0].source_snapshot
    category = items[0].canonical_category
    return CategoryCard(
        card_id=_stable_card_id(category, "price_range", "all", snapshot),
        category=category,
        card_type="price_range",
        summary=(
            f"便宜款 {low:.2f}-{q33:.2f} / 中档 {q33:.2f}-{q67:.2f} / "
            f"高端 {q67:.2f}-{high:.2f}（当前快照挂牌价）"
        ),
        raw_evidence=[
            _compact(f"有效CNY挂牌价={len(prices)}/{len(items)}；q33={q33:.2f}；q67={q67:.2f}"),
            _compact(f"挂牌价范围={low:.2f}-{high:.2f}；非历史成交价"),
        ],
        last_updated=items[0].observed_at,
        confidence=round(confidence, 6),
    )


def _bestseller_cards(
    items: list[NormalizedEvidence], *, min_confidence: float
) -> list[CategoryCard]:
    grouped: dict[str, list[NormalizedEvidence]] = defaultdict(list)
    for item in items:
        grouped[item.platform].append(item)

    cards: list[CategoryCard] = []
    for platform, platform_items in sorted(grouped.items()):
        eligible = [
            item for item in platform_items if item.sales is not None or item.rating is not None
        ]
        confidence = len(eligible) / len(platform_items) if platform_items else 0.0
        if not eligible or confidence < min_confidence:
            continue
        by_sales = sorted(
            (item for item in eligible if item.sales is not None),
            key=lambda item: (-(item.sales or 0), item.item_id),
        )[:3]
        by_rating = sorted(
            (item for item in eligible if item.rating is not None),
            key=lambda item: (-(item.rating or 0), item.item_id),
        )[:3]
        selected: list[NormalizedEvidence] = []
        seen: set[str] = set()
        for item in [*by_sales, *by_rating]:
            if item.item_id not in seen:
                seen.add(item.item_id)
                selected.append(item)
            if len(selected) == 5:
                break
        if not selected:
            continue
        category = platform_items[0].canonical_category
        snapshot = platform_items[0].source_snapshot
        names = " / ".join(_compact(item.title, 60) for item in selected)
        cards.append(
            CategoryCard(
                card_id=_stable_card_id(category, "bestseller", platform, snapshot),
                category=category,
                card_type="bestseller",
                summary=f"{category}（{platform}平台内候选）: {names}",
                raw_evidence=[_facts_text(item) for item in selected[:3]],
                last_updated=platform_items[0].observed_at,
                confidence=round(confidence, 6),
            )
        )
    return cards


def _searchable_text(item: NormalizedEvidence) -> str:
    return (item.title + " " + json.dumps(item.attributes, ensure_ascii=False)).lower()


def _attribute_cards(
    items: list[NormalizedEvidence], *, min_confidence: float
) -> list[CategoryCard]:
    if not items:
        return []
    cards: list[CategoryCard] = []
    category = items[0].canonical_category
    snapshot = items[0].source_snapshot
    for dimension, rules in ATTRIBUTE_RULES.items():
        counts: Counter[str] = Counter()
        identified = 0
        for item in items:
            text = _searchable_text(item)
            label = next(
                (
                    candidate
                    for candidate, tokens in rules
                    if any(token.lower() in text for token in tokens)
                ),
                "unknown",
            )
            counts[label] += 1
            if label != "unknown":
                identified += 1
        confidence = identified / len(items)
        if confidence < min_confidence:
            continue
        ordered_labels = [label for label, _ in rules]
        if counts["unknown"]:
            ordered_labels.append("unknown")
        summary_parts = [
            f"{label} {counts[label] / len(items):.1%}"
            for label in ordered_labels
            if counts[label]
        ]
        evidence_parts = [f"{label}={counts[label]}" for label in ordered_labels if counts[label]]
        cards.append(
            CategoryCard(
                card_id=_stable_card_id(category, "attribute", dimension, snapshot),
                category=category,
                card_type="attribute",
                summary=f"{dimension}: " + " / ".join(summary_parts),
                raw_evidence=[
                    _compact(
                        f"样本={len(items)}；可识别={identified}；"
                        + "；".join(evidence_parts)
                    )
                ],
                last_updated=items[0].observed_at,
                confidence=round(confidence, 6),
            )
        )
    return cards


def build_category_card_drafts(
    evidence: Iterable[NormalizedEvidence], *, min_confidence: float = 0.5
) -> list[CategoryCard]:
    """Aggregate normalized evidence into deterministic, auditable card drafts."""

    items = list(evidence)
    if not items:
        return []
    categories = {item.canonical_category for item in items}
    snapshots = {item.source_snapshot for item in items}
    if len(categories) != 1 or len(snapshots) != 1:
        raise ValueError("一次制卡只能处理一个规范品类和一个源快照")

    cards = _bestseller_cards(items, min_confidence=min_confidence)
    cards.extend(_attribute_cards(items, min_confidence=min_confidence))
    price_card = _price_card(items, min_confidence=min_confidence)
    if price_card is not None:
        cards.append(price_card)
    return sorted(cards, key=lambda card: (card.card_type, card.card_id))
