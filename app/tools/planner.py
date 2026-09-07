"""Intent decomposition for shopping tasks.

The model-facing argument schema is deliberately tolerant: structured LLM output
often omits fields that the strict ``ShoppingIntent`` validator requires (for
example ``category_key`` / ``category_name``) or invents keys it forbids. The
strict contract stays the single source of truth, but this tool repairs obvious
missing derivable fields first and degrades to an explicit ``invalid_intent``
result instead of surfacing a raw pydantic exception to the model.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from app.products.catalog.intent import ProductIdentity, ShoppingIntent
from app.search.schemas import SearchFilters


def _build_lenient_intent() -> type[BaseModel]:
    """Mirror ShoppingIntent's JSON schema with every field optional and lax.

    extra="ignore" absorbs keys the model invents; unknown nested keys survive as
    plain dicts here and are pruned before the strict validation step.
    """

    schema = ShoppingIntent.model_json_schema()
    properties = schema.get("properties", {})
    fields: dict[str, Any] = {}
    for name, prop in properties.items():
        kind = prop.get("type")
        if kind == "string":
            annotation: Any = str
        elif kind == "boolean":
            annotation = bool
        elif kind in {"integer", "number"}:
            annotation = int
        elif kind == "array":
            annotation = list
        elif kind == "object" or "$ref" in prop:
            annotation = dict
        else:
            annotation = Any
        fields[name] = (
            annotation | None,
            Field(default=None, description=prop.get("description")),
        )
    return create_model(
        "LenientShoppingIntent",
        __config__=ConfigDict(extra="ignore"),
        **fields,
    )


LenientShoppingIntent = _build_lenient_intent()


def _category_key_from(text: str) -> str:
    """Deterministic identity key derived from user/query text (no taxonomy)."""

    import re

    key = re.sub(r"[^\w]+", "_", text.lower()).strip("_")[:128]
    return key or "general"


def _repair(goal: str, raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Fill missing derivable intent fields; return cleaned data and repair labels."""

    repaired: list[str] = []
    cleaned = {key: value for key, value in raw.items() if key in ShoppingIntent.model_fields}

    mode = str(cleaned.get("intent_mode") or "category_explore")
    needs_clarification = bool(cleaned.get("needs_clarification"))
    goal_text = goal.strip()

    if mode in {"category_explore", "exact_product"} and not needs_clarification:
        primary_query = str(cleaned.get("primary_query") or "").strip()
        if not primary_query:
            primary_query = goal_text
            cleaned["primary_query"] = primary_query
            repaired.append("primary_query")
        if not str(cleaned.get("category_name") or "").strip():
            cleaned["category_name"] = primary_query[:128]
            repaired.append("category_name")
        if not str(cleaned.get("category_key") or "").strip():
            cleaned["category_key"] = _category_key_from(primary_query)
            repaired.append("category_key")

    if cleaned.get("intent_mode") == "goal_explore" and not needs_clarification:
        # Contradictory output: goal_explore must always clarify first.
        cleaned["needs_clarification"] = True
        if not str(cleaned.get("clarification_question") or "").strip():
            cleaned["clarification_question"] = f"请补充您想买的商品品类或具体型号：{goal_text}"
            repaired.append("clarification_question")

    if needs_clarification and not str(cleaned.get("clarification_question") or "").strip():
        cleaned["clarification_question"] = "还需要补充哪些信息？"
        repaired.append("clarification_question")

    filters = cleaned.get("filters")
    if isinstance(filters, dict):
        cleaned["filters"] = {
            key: value for key, value in filters.items() if key in SearchFilters.model_fields
        }
    identity = cleaned.get("product_identity")
    if isinstance(identity, dict):
        cleaned["product_identity"] = {
            key: value
            for key, value in identity.items()
            if key in ProductIdentity.model_fields
        }
    for key, maximum in (
        ("query_variants", 2),
        ("hard_constraints", 20),
        ("soft_preferences", 20),
        ("blocked_item_ids", 100),
    ):
        value = cleaned.get(key)
        if isinstance(value, list) and len(value) > maximum:
            cleaned[key] = value[:maximum]
            repaired.append(key)
    return cleaned, repaired


@tool
def planner(goal: str, shopping_intent: LenientShoppingIntent | None = None) -> dict:
    """Split a shopping goal into ordered, tool-oriented execution steps."""

    normalized = goal.strip()
    steps = [
        {
            "order": 1,
            "action": "clarify_constraints",
            "tool": "chat_fallback",
            "when": "预算、品类或硬约束缺失时",
        },
        {"order": 2, "action": "search_candidates", "tool": "item_search"},
        {
            "order": 3,
            "action": "shortlist",
            "tool": "item_picker",
            "when": "候选需要按明确约束筛选时",
        },
        {
            "order": 4,
            "action": "compare_total_cost",
            "tool": "price_compare",
            "when": "商品价和运费均有真实来源时",
        },
        {"order": 5, "action": "summarize", "tool": "shopping_summary"},
    ]
    raw = (
        shopping_intent.model_dump(exclude_none=True)
        if shopping_intent is not None
        else {}
    )
    if not raw:
        return {
            "status": "needs_planning",
            "goal": normalized,
            "shopping_intent": None,
            "steps": steps,
        }

    cleaned, repaired = _repair(normalized, raw)
    try:
        strict = ShoppingIntent.model_validate(cleaned)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        return {
            "status": "invalid_intent",
            "goal": normalized,
            "shopping_intent": None,
            "steps": steps,
            "message": "购物意图缺少必要字段，未生成可执行计划；请补充明确信息后重试。",
            "detail": f"{first.get('loc')}: {first.get('msg', '')}"[:300],
        }

    status = (
        "insufficient_intent"
        if strict.intent_mode == "goal_explore" and strict.clarification_count >= 2
        else "needs_clarification"
        if strict.needs_clarification
        else "ok"
    )
    payload: dict[str, Any] = {
        "status": status,
        "goal": normalized,
        "shopping_intent": strict.model_dump(mode="json"),
        "steps": steps,
        "note": "品类和预算明确时先做宽泛检索；性别、版型、颜色和品牌可在结果后继续细化。",
    }
    if repaired:
        payload["repaired"] = True
        payload["repaired_fields"] = repaired
        payload["degraded_reason"] = "planner_repaired_incomplete_intent"
    return payload
