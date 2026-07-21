"""Shared deterministic cost calculations for domestic offers."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

MONEY_QUANTUM = Decimal("0.01")


def _money(value: Any, *, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是有效数字") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field} 必须是非负有限数字")
    return number.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def calculate_domestic_cost(
    *,
    item_price: Any,
    quantity: int = 1,
    shipping_fee: Any | None = None,
    currency: str = "CNY",
) -> dict[str, Any]:
    """Calculate one domestic offer without inventing an unknown shipping fee."""

    normalized_currency = currency.strip().upper()
    if normalized_currency != "CNY":
        raise ValueError("国内费用计算首期只接受 CNY")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("quantity 必须是正整数")

    price = _money(item_price, field="item_price")
    merchandise = (price * quantity).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    if shipping_fee is None:
        return {
            "status": "insufficient_data",
            "currency": normalized_currency,
            "merchandise_cost": float(merchandise),
            "shipping_fee": None,
            "total_cost": None,
            "formula": "item_price × quantity + shipping_fee",
            "message": "运费未知，不能按包邮或零元运费计算总价。",
        }

    shipping = _money(shipping_fee, field="shipping_fee")
    total = (merchandise + shipping).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    return {
        "status": "ok",
        "currency": normalized_currency,
        "merchandise_cost": float(merchandise),
        "shipping_fee": float(shipping),
        "total_cost": float(total),
        "formula": "item_price × quantity + shipping_fee",
        "message": "按已提供的国内商品价、数量和运费计算。",
    }
