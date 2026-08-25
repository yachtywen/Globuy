import pytest
from pydantic import ValidationError

from app.tools.price_compare import price_compare


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
