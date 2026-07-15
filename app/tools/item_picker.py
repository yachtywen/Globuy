"""Deterministic candidate re-ranking."""

from typing import Any

from langchain_core.tools import tool


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@tool
def item_picker(items: list[dict[str, Any]], limit: int = 5) -> dict:
    """Rank candidate items by explicit score, rating, and price signals."""

    def rank(item: dict[str, Any]) -> tuple[float, float, float]:
        return (
            _number(item.get("score")),
            _number(item.get("rating")),
            -_number(item.get("price"), float("inf")),
        )

    selected = sorted(items, key=rank, reverse=True)[: max(1, min(limit, 20))]
    return {
        "status": "ok",
        "input_count": len(items),
        "selected": selected,
        "ranking_rule": "score desc, rating desc, price asc",
    }
