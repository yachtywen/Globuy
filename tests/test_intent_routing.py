from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.products.catalog.intent import ShoppingIntent
from app.products.grouping import group_candidates
from app.recall.transient_hybrid import (
    TransientFaissFlatIndex,
    _balanced,
    bm25_order,
    tokenize_bm25,
)
from app.search.schemas import Candidate


def candidate(item_id: str, platform: str, rank: int, *, title: str) -> Candidate:
    return Candidate(
        item_id=item_id,
        product_id=item_id,
        offer_id=f"{platform}:{item_id}",
        platform=platform,
        title=title,
        price=100,
        currency="CNY",
        product_url=f"https://example.com/{platform}/{item_id}",
        source_rank=rank,
    )


def test_category_intent_derives_backward_compatible_queries() -> None:
    intent = ShoppingIntent(
        category_key="headphones",
        category_name="耳机",
        primary_query="头戴式降噪耳机",
        platforms=["taobao"],
        soft_preferences=["适合通勤"],
    )
    assert intent.intent_mode == "category_explore"
    assert intent.lexical_query == "头戴式降噪耳机"
    assert intent.semantic_query == "头戴式降噪耳机 适合通勤"


def test_goal_intent_blocks_search_until_one_category_is_resolved() -> None:
    intent = ShoppingIntent(
        intent_mode="goal_explore",
        intent_confidence="low",
        platforms=["taobao", "jingdong", "douyin"],
        needs_clarification=True,
        clarification_question="她更喜欢数码、香氛还是首饰？",
        clarification_count=1,
    )
    assert intent.provider_allowed is False
    with pytest.raises(ValidationError):
        ShoppingIntent(
            intent_mode="goal_explore",
            platforms=["taobao"],
            needs_clarification=False,
        )


def test_exact_intent_requires_model_or_source_item_id() -> None:
    with pytest.raises(ValidationError):
        ShoppingIntent(
            intent_mode="exact_product",
            product_identity={"brand": "Sony"},
            category_key="headphones",
            category_name="耳机",
            primary_query="Sony 耳机",
            platforms=["taobao"],
        )


def test_bm25_preserves_model_tokens_and_chinese_bigrams() -> None:
    tokens = tokenize_bm25("Sony WH-1000XM5 头戴式降噪耳机")
    assert "wh-1000xm5" in tokens
    assert "降噪" in tokens
    groups, _ = group_candidates(
        [
            candidate("a", "taobao", 1, title="Sony WH-1000XM5 头戴式耳机"),
            candidate("b", "jingdong", 1, title="普通无线耳机"),
        ]
    )
    assert bm25_order(groups, "WH-1000XM5")[0].representative.item_id == "a"


def test_transient_faiss_flat_returns_exact_cosine_order() -> None:
    index = TransientFaissFlatIndex(3)
    index.add([[1, 0, 0], [0, 1, 0], [0.8, 0.2, 0]])
    results = index.search([1, 0, 0], 3)
    assert [position for position, _ in results] == [0, 2, 1]


def test_platform_balance_uses_a_floor_then_preserves_global_rank() -> None:
    candidates = [
        candidate(f"taobao-{index}", "taobao", index + 1, title=f"相关商品 {index}")
        for index in range(8)
    ]
    candidates.extend(
        candidate(f"jd-{index}", "jingdong", index + 1, title=f"京东商品 {index}")
        for index in range(4)
    )
    candidates.extend(
        candidate(f"dy-{index}", "douyin", index + 1, title=f"抖音商品 {index}")
        for index in range(4)
    )
    groups, _ = group_candidates(candidates)
    selected = _balanced(groups, 12)
    counts = {
        platform: sum(group.representative.platform == platform for group in selected)
        for platform in ("taobao", "jingdong", "douyin")
    }
    assert counts["jingdong"] >= 2
    assert counts["douyin"] >= 2
    protected_ids = {group.product_group_id for group in selected[:6]}
    expected_tail = [
        group.product_group_id for group in groups if group.product_group_id not in protected_ids
    ][:6]
    assert [group.product_group_id for group in selected[6:]] == expected_tail
