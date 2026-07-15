"""Chapter 4: run a deterministic pick -> price -> shipping pipeline."""

from pprint import pprint

from app.tools.item_picker import item_picker
from app.tools.price_compare import price_compare
from app.tools.shipping_calc import shipping_calc

offers = [
    {"title": "候选 A", "score": 0.82, "rating": 4.8, "price": 899, "shipping_fee": 30},
    {"title": "候选 B", "score": 0.90, "rating": 4.5, "price": 929, "shipping_fee": 0},
]

shortlist = item_picker.invoke({"items": offers, "limit": 2})
comparison = price_compare.invoke({"items": shortlist["selected"], "currency": "CNY"})
landed_cost = shipping_calc.invoke(
    {"item_price": comparison["best_offer"]["price"], "shipping_fee": 0}
)
pprint({"shortlist": shortlist, "comparison": comparison, "landed_cost": landed_cost})
