"""Intent decomposition for shopping tasks."""

from langchain_core.tools import tool

from app.products.catalog.intent import ShoppingIntent


@tool
def planner(goal: str, shopping_intent: ShoppingIntent | None = None) -> dict:
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
        "status": (
            "insufficient_intent"
            if shopping_intent is not None
            and shopping_intent.intent_mode == "goal_explore"
            and shopping_intent.clarification_count >= 2
            else "needs_clarification"
            if shopping_intent is not None and shopping_intent.needs_clarification
            else "ok"
            if shopping_intent is not None
            else "needs_planning"
        ),
        "goal": normalized,
        "shopping_intent": shopping_intent.model_dump(mode="json") if shopping_intent else None,
        "steps": steps,
        "note": "品类和预算明确时先做宽泛检索；性别、版型、颜色和品牌可在结果后继续细化。",
    }
