"""Shipping, duty and landed-cost estimation."""

from typing import Any

from langchain_core.tools import tool


@tool
def shipping_calc(
    item_price: float,
    quantity: int = 1,
    shipping_fee: float = 0,
    duty_rate: float = 0,
    extra_fees: float = 0,
    currency: str = "CNY",
) -> dict[str, Any]:
    """Estimate landed cost from item, shipping, duty, and extra fees."""

    quantity = max(quantity, 1)
    merchandise = round(max(item_price, 0) * quantity, 2)
    duty = round(merchandise * max(duty_rate, 0), 2)
    total = round(merchandise + max(shipping_fee, 0) + duty + max(extra_fees, 0), 2)
    return {
        "status": "estimate",
        "currency": currency.upper(),
        "merchandise": merchandise,
        "shipping_fee": max(shipping_fee, 0),
        "estimated_duty": duty,
        "extra_fees": max(extra_fees, 0),
        "total": total,
        "warning": "税率和清关费用为输入估算值，不构成海关或税务意见。",
    }
