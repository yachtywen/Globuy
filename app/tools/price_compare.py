"""Deterministic comparison of already-normalized domestic offers."""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from app.tools.costs import calculate_domestic_cost


class OfferInput(BaseModel):
    """One normalized offer accepted by PriceCompare."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = ""
    platform: str = ""
    title: str = ""
    price: float = Field(ge=0)
    quantity: int = Field(default=1, ge=1)
    shipping_fee: float | None = Field(default=None, ge=0)
    currency: Literal["CNY"] = "CNY"
    product_url: str | None = None
    retrieval_rank: int | None = Field(default=None, ge=1)


def _sort_key(item: dict[str, Any]) -> tuple[float, float, str, str]:
    rank = item.get("retrieval_rank")
    return (
        float(item["total_cost"]),
        float(rank) if rank is not None else float("inf"),
        str(item.get("platform") or ""),
        str(item.get("item_id") or ""),
    )


@tool
def price_compare(items: list[OfferInput], currency: Literal["CNY"] = "CNY") -> dict:
    """Compare offers with known shipping; keep unknown-shipping offers separate."""

    comparable: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    for item in items:
        offer = item.model_dump(mode="json") if isinstance(item, OfferInput) else dict(item)
        if offer["currency"] != currency:
            raise ValueError("所有报价币种必须与 price_compare.currency 一致")
        cost = calculate_domestic_cost(
            item_price=offer["price"],
            quantity=offer["quantity"],
            shipping_fee=offer.get("shipping_fee"),
            currency=offer["currency"],
        )
        enriched = {**offer, **cost}
        if cost["status"] == "ok":
            comparable.append(enriched)
        else:
            incomplete.append(enriched)

    comparable.sort(key=_sort_key)
    best = comparable[0] if comparable else None
    if not comparable:
        status = "insufficient_data"
    elif incomplete:
        status = "partial"
    else:
        status = "ok"
    return {
        "status": status,
        "currency": currency,
        "offers": comparable,
        "incomplete_offers": incomplete,
        "best_offer": best,
        "message": (
            "仅有完整运费的报价参与最低总价判断。"
            if incomplete
            else "所有报价均按商品价、数量和已知运费比较。"
        ),
    }
