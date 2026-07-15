"""Fuse user/query affinity and rank item vectors."""

from collections.abc import Mapping, Sequence
from typing import Any

from app.recall.tower_item import ItemTower
from app.recall.tower_query import QueryTower
from app.recall.tower_user import UserTower


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


def rank_items(
    profile: Mapping[str, Any],
    query: str,
    items: Sequence[Mapping[str, Any]],
    *,
    user_weight: float = 0.35,
) -> list[dict[str, Any]]:
    user_vector = UserTower().encode(profile)
    query_vector = QueryTower().encode(query)
    item_tower = ItemTower()
    ranked = []
    for item in items:
        item_vector = item_tower.encode(item)
        score = user_weight * cosine(user_vector, item_vector) + (1 - user_weight) * cosine(
            query_vector, item_vector
        )
        ranked.append({**item, "recall_score": round(score, 6)})
    return sorted(ranked, key=lambda value: value["recall_score"], reverse=True)
