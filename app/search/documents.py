"""Deterministic catalog-document construction."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from app.products.identity import offer_id, product_id
from app.search.schemas import Scalar

STABLE_SEMANTIC_KEYS = frozenset(
    {
        "brand",
        "model",
        "category",
        "category_path",
        "wearing_style",
        "connection_type",
        "noise_cancellation",
        "use_case",
    }
)


def canonical_scalar(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(value, ".15g")
    return str(value).strip()


def attribute_term(key: str, value: Scalar) -> str:
    return f"{key.strip()}={canonical_scalar(value)}"


def _attribute_terms(value: Any, prefix: str = "") -> Iterable[str]:
    if isinstance(value, Mapping):
        for key in sorted(value):
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _attribute_terms(value[key], path)
    elif isinstance(value, list):
        for item in value:
            yield from _attribute_terms(item, prefix)
    elif isinstance(value, (str, int, float, bool)) and prefix:
        yield attribute_term(prefix, value)


def flatten_attribute_terms(attributes: Mapping[str, Any]) -> list[str]:
    return sorted(set(_attribute_terms(attributes)))


def _semantic_values(value: Any) -> Iterable[str]:
    if isinstance(value, list):
        for item in value:
            yield from _semantic_values(item)
    elif isinstance(value, (str, int, float, bool)):
        text = str(value).strip()
        if text:
            yield text


def semantic_text(item: Mapping[str, Any]) -> str:
    parts = [str(item.get("title") or "").strip()]
    attributes = item.get("attributes")
    if isinstance(attributes, Mapping):
        for key in sorted(STABLE_SEMANTIC_KEYS):
            if key in attributes:
                parts.extend(_semantic_values(attributes[key]))
    return "\n".join(dict.fromkeys(part for part in parts if part))


def index_document(item: Mapping[str, Any], vector: list[float]) -> dict[str, Any]:
    attributes = item.get("attributes")
    if not isinstance(attributes, Mapping):
        attributes = {}
    return {
        "item_id": str(item["item_id"]),
        "product_id": str(item.get("product_id") or product_id(str(item["item_id"]))),
        "offer_id": str(item.get("offer_id") or offer_id(str(item["item_id"]))),
        "platform": str(item["platform"]),
        "title": str(item["title"]),
        "price": float(item["price"]),
        "currency": str(item["currency"]),
        "rating": item.get("rating"),
        "sales": item.get("sales"),
        "image_url": item.get("image_url"),
        "attributes": json.loads(json.dumps(attributes, ensure_ascii=False)),
        "attribute_terms": flatten_attribute_terms(attributes),
        "product_url": item.get("product_url"),
        "semantic_text": semantic_text(item),
        "content_vector": vector,
    }
