"""Chapter 5: rank local candidates with user/query/item towers."""

from pprint import pprint

from app.recall import rank_items

profile = {"budget": "1000", "category": "耳机", "preference": "通勤 降噪 轻便"}
items = [
    {"title": "轻便通勤降噪耳机", "category": "耳机", "features": "降噪 轻便"},
    {"title": "桌面监听耳机", "category": "耳机", "features": "有线 高保真"},
]
pprint(rank_items(profile, "通勤降噪耳机", items))
