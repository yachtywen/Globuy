"""Intent decomposition for shopping tasks."""

from langchain_core.tools import tool


@tool
def planner(goal: str) -> dict:
    """Split a shopping goal into ordered, tool-oriented execution steps."""

    normalized = goal.strip()
    steps = [
        {
            "order": 1,
            "action": "clarify_constraints",
            "tool": "chat_fallback",
            "when": "预算、品类或硬约束缺失时",
        },
        {
            "order": 2,
            "action": "learn_category",
            "tool": "category_insight",
            "when": "需要拆子品类、理解典型属性或价格档位时",
        },
        {"order": 3, "action": "search_candidates", "tool": "item_search"},
        {
            "order": 4,
            "action": "shortlist",
            "tool": "item_picker",
            "when": "候选需要按明确约束筛选时",
        },
        {
            "order": 5,
            "action": "compare_total_cost",
            "tool": "price_compare",
            "when": "商品价和运费均有真实来源时",
        },
        {"order": 6, "action": "summarize", "tool": "shopping_summary"},
    ]
    return {
        "status": "ok",
        "goal": normalized,
        "steps": steps,
        "note": "执行前应补齐预算、目的地、偏好和时间要求。",
    }
