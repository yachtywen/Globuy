"""Final shopping manifest generation."""

from typing import Any

from langchain_core.tools import tool


@tool
def shopping_summary(
    goal: str,
    selected_items: list[dict[str, Any]],
    unresolved: list[str] | None = None,
) -> dict:
    """Build a final machine-readable shopping manifest."""

    return {
        "status": "complete" if selected_items else "incomplete",
        "goal": goal.strip(),
        "selected_items": selected_items,
        "unresolved": unresolved or [],
        "item_count": len(selected_items),
        "disclaimer": "下单前需要重新核验价格、库存、运费、税费和售后政策。",
    }
