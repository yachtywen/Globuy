import pytest
from pydantic import ValidationError

from app.tools.price_compare import price_compare
from app.tools.shipping_calc import shipping_calc


def test_shipping_calc_keeps_unknown_shipping_out_of_total() -> None:
    result = shipping_calc.invoke({"item_price": 99.995, "quantity": 2})

    assert result["status"] == "insufficient_data"
    assert result["merchandise_cost"] == 200.0
    assert result["shipping_fee"] is None
    assert result["total_cost"] is None


def test_shipping_calc_uses_decimal_money_rounding() -> None:
    result = shipping_calc.invoke(
        {"item_price": 10.005, "quantity": 3, "shipping_fee": 2.005}
    )

    assert result["status"] == "ok"
    assert result["merchandise_cost"] == 30.03
    assert result["shipping_fee"] == 2.01
    assert result["total_cost"] == 32.04


def test_shipping_calc_rejects_removed_cross_border_fields() -> None:
    with pytest.raises(ValidationError, match="duty_rate"):
        shipping_calc.invoke(
            {
                "item_price": 100,
                "shipping_fee": 0,
                "duty_rate": 0.1,
            }
        )


def test_price_compare_excludes_unknown_shipping_from_winner() -> None:
    result = price_compare.invoke(
        {
            "items": [
                {
                    "item_id": "unknown-cheap",
                    "price": 50,
                    "shipping_fee": None,
                    "retrieval_rank": 1,
                },
                {
                    "item_id": "complete",
                    "price": 80,
                    "shipping_fee": 5,
                    "retrieval_rank": 2,
                },
            ]
        }
    )

    assert result["status"] == "partial"
    assert result["best_offer"]["item_id"] == "complete"
    assert result["best_offer"]["total_cost"] == 85.0
    assert result["incomplete_offers"][0]["item_id"] == "unknown-cheap"


def test_price_compare_rejects_legacy_offer_fields() -> None:
    with pytest.raises(ValidationError, match="tax_rate"):
        price_compare.invoke(
            {
                "items": [
                    {
                        "price": 100,
                        "shipping_fee": 0,
                        "tax_rate": 0.1,
                    }
                ]
            }
        )
