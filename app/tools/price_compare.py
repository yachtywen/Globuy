"""Price normalization and comparison."""

from typing import Any

from langchain_core.tools import tool


def _total(item: dict[str, Any]) -> float | None:
    try:
        return round(
            float(item["price"]) + float(item.get("shipping_fee", 0)) + float(item.get("tax", 0)),
            2,
        )
    except (KeyError, TypeError, ValueError):
        return None


@tool
def price_compare(items: list[dict[str, Any]], currency: str = "CNY") -> dict:
    """Compare already-normalized offers using total landed cost."""

    comparable = []
    for item in items:
        total = _total(item)
        if total is not None:
            comparable.append({**item, "total_cost": total})
    comparable.sort(key=lambda item: item["total_cost"])
    best = comparable[0] if comparable else None
    return {
        "status": "ok" if comparable else "insufficient_data",
        "currency": currency.upper(),
        "offers": comparable,
        "best_offer": best,
        "warning": "调用前必须先把不同币种换算为同一币种。",
    }
