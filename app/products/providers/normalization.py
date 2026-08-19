"""Category-neutral normalization for Just One search responses."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.search.schemas import Platform


def _nested(value: Any, names: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 5:
        return None
    lowered = {name.lower() for name in names}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in lowered and item not in (None, "", [], {}):
                return item
        for child in value.values():
            found = _nested(child, names, depth + 1)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(value, list):
        for child in value[:5]:
            found = _nested(child, names, depth + 1)
            if found not in (None, "", [], {}):
                return found
    return None


def _money(value: Any, *, cents: bool = False) -> float | None:
    if isinstance(value, dict):
        value = _nested(value, ("origin", "price", "amount", "value"))
    if isinstance(value, list):
        value = next((item for item in value if item not in (None, "")), None)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if match is None:
        return None
    try:
        result = Decimal(match.group())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if cents:
        result /= 100
    if result < 0:
        return None
    return float(result.quantize(Decimal("0.01")))


def _integer(value: Any) -> int | None:
    if isinstance(value, dict):
        value = _nested(value, ("origin", "value", "count"))
    try:
        text = str(value).replace(",", "").replace("+", "").strip().lower()
        multiplier = 100_000_000 if "亿" in text else 10_000 if "万" in text else 1
        match = re.search(r"\d+(?:\.\d+)?", text)
        return max(0, int(float(match.group()) * multiplier)) if match else None
    except (TypeError, ValueError):
        return None


def _absolute_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    if url.startswith("//"):
        return "https:" + url
    return url if url.startswith(("http://", "https://")) else None


def _image_url(value: Any) -> str | None:
    direct = _absolute_url(value)
    if direct:
        return direct
    if isinstance(value, list):
        for item in value:
            if result := _image_url(item):
                return result
    if isinstance(value, dict):
        for key in ("url", "url_list", "imageUrl", "image_url", "src", "origin"):
            if result := _image_url(value.get(key)):
                return result
    return None


def _jd_image_url(value: Any) -> str | None:
    if result := _image_url(value):
        return result
    if isinstance(value, str) and value.strip().lstrip("/").startswith("jfs/"):
        return "https://img10.360buyimg.com/n1/" + value.strip().lstrip("/")
    return None


def extract_items(
    payload: dict[str, Any], platform: Platform | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    metadata: dict[str, Any] = {}
    items: Any = []
    if platform == "taobao" and isinstance(data, dict):
        model = data.get("model") if isinstance(data.get("model"), dict) else {}
        items = model.get("itemList") or []
        page = model.get("page") if isinstance(model.get("page"), dict) else {}
        metadata["total_pages"] = _integer(page.get("totalPages"))
    elif platform == "jingdong" and isinstance(data, dict):
        items = data.get("products") or []
        metadata["total_pages"] = _integer(data.get("totalPages"))
    elif platform == "douyin" and isinstance(data, dict):
        items = data.get("summary_promotions") or data.get("promotions") or []
        extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
        metadata["search_id"] = (
            extra.get("search_id")
            or extra.get("searchId")
            or _nested(data, ("searchId", "search_id", "search_id_str"))
        )
        metadata["total_pages"] = _integer(
            _nested(data, ("totalPage", "totalPages", "total_pages", "pageCount"))
        )
        metadata["has_more"] = data.get("has_more")
    if isinstance(items, list) and items:
        return [item for item in items if isinstance(item, dict)], metadata

    arrays: list[list[dict[str, Any]]] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            arrays.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                visit(child, depth + 1)

    visit(data)
    items = max(
        arrays,
        key=lambda rows: sum(
            bool(
                _nested(
                    row,
                    ("itemId", "item_id", "skuId", "productId", "product_id", "id"),
                )
            )
            and bool(
                _nested(
                    row,
                    ("title", "itemTitle", "itemName", "name", "productName", "goodsName"),
                )
            )
            for row in rows[:5]
        ),
        default=[],
    )
    metadata.setdefault("search_id", _nested(data, ("searchId", "search_id", "search_id_str")))
    metadata.setdefault(
        "total_pages",
        _integer(
            _nested(data, ("totalPage", "totalPages", "total_pages", "pageCount"))
        ),
    )
    metadata.setdefault("has_more", _nested(data, ("has_more", "hasMore")))
    return items, metadata


def _clean_attributes(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "", [], {})}


def _normalize_taobao(item: dict[str, Any]) -> dict[str, Any]:
    source_id = item.get("itemId") or item.get("prodId")
    return {
        "source_id": source_id,
        "title": item.get("itemName") or item.get("itemSubName"),
        "price": _money(
            item.get("discntPriceYuan")
            or item.get("priceZKYuanDouble")
            or item.get("priceYuanDouble")
        ),
        "rating": _money(item.get("itemGradeAvg")),
        "sales": _integer(item.get("orderPayUV")),
        "image_url": _image_url(item.get("picUrlFull") or item.get("picUrl")),
        "product_url": (
            f"https://item.taobao.com/item.htm?id={source_id}" if source_id else None
        ),
        "attributes": _clean_attributes(
            {
                "shop_id": item.get("shopId"),
                "shop_name": item.get("shopName"),
                "seller_location": item.get("sellerLoc") or item.get("itemLoc"),
                "options": item.get("options"),
                "comment_count": _integer(item.get("commentCount")),
            }
        ),
    }


def _normalize_jingdong(item: dict[str, Any]) -> dict[str, Any]:
    source_id = item.get("id") or item.get("oneItemId") or item.get("skuId")
    product_url = _absolute_url(item.get("landUrl"))
    if product_url is None and source_id:
        product_url = f"https://item.jd.com/{source_id}.html"
    return {
        "source_id": source_id,
        "title": item.get("title") or item.get("shortTitle"),
        "price": _money(item.get("price") or item.get("lowestPrice")),
        "rating": None,
        "sales": _integer(item.get("sales") or item.get("monthSales")),
        "image_url": _jd_image_url(
            item.get("imageUrl") or item.get("longImageUrl") or item.get("images")
        ),
        "product_url": product_url,
        "attributes": _clean_attributes(
            {
                "shop_id": item.get("shopId"),
                "shop_name": item.get("shopName"),
                "category_1": item.get("cid1"),
                "category_2": item.get("cid2"),
                "category_3": item.get("cid3"),
                "structured_attributes": item.get("asgStructuredAttribute"),
            }
        ),
    }


def _normalize_douyin(item: dict[str, Any]) -> dict[str, Any]:
    base = item.get("base_model") if isinstance(item.get("base_model"), dict) else {}
    product = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
    marketing = (
        base.get("marketing_info") if isinstance(base.get("marketing_info"), dict) else {}
    )
    price_desc = (
        marketing.get("price_desc") if isinstance(marketing.get("price_desc"), dict) else {}
    )
    shop = base.get("shop_info") if isinstance(base.get("shop_info"), dict) else {}
    category = product.get("category") if isinstance(product.get("category"), dict) else {}
    category_path = [
        str(value["category_name"])
        for key in ("first_category", "second_category", "third_category", "fourth_category")
        if isinstance((value := category.get(key)), dict) and value.get("category_name")
    ]
    return {
        "source_id": item.get("product_id") or item.get("promotion_id"),
        "title": product.get("name"),
        "price": _money(price_desc.get("price"), cents=True),
        "rating": _money(product.get("good_ratio")),
        "sales": _integer(product.get("month_sale")),
        "image_url": _image_url(product.get("main_img") or product.get("white_img")),
        "product_url": _absolute_url(product.get("detail_url")),
        "attributes": _clean_attributes(
            {
                "promotion_id": item.get("promotion_id"),
                "shop_id": shop.get("shop_id"),
                "shop_name": shop.get("shop_name"),
                "category_path": category_path,
            }
        ),
    }


def normalize_item(platform: Platform, item: dict[str, Any]) -> dict[str, Any] | None:
    mapped = (
        _normalize_taobao(item)
        if platform == "taobao"
        else _normalize_jingdong(item)
        if platform == "jingdong"
        else _normalize_douyin(item)
        if platform == "douyin" and isinstance(item.get("base_model"), dict)
        else {
            "source_id": _nested(
                item,
                (
                    "itemId",
                    "item_id",
                    "skuId",
                    "sku_id",
                    "productId",
                    "product_id",
                    "id",
                ),
            ),
            "title": _nested(
                item,
                ("title", "itemTitle", "itemName", "skuName", "productName", "goodsName", "name"),
            ),
            "price": _money(
                _nested(
                    item,
                    (
                        "price",
                        "priceInfo",
                        "salePrice",
                        "currentPrice",
                        "priceInfoVO",
                        "priceYuanDouble",
                        "discntPriceYuan",
                    ),
                ),
                cents=platform == "douyin",
            ),
            "product_url": _absolute_url(
                _nested(
                    item,
                    (
                        "productUrl",
                        "product_url",
                        "itemUrl",
                        "detailUrl",
                        "detail_url",
                        "url",
                        "shareUrl",
                    ),
                )
            ),
            "image_url": _image_url(
                _nested(
                    item,
                    (
                        "imageUrl",
                        "image_url",
                        "image",
                        "picUrl",
                        "picUrlFull",
                        "mainImage",
                        "cover",
                    ),
                )
            ),
            "rating": _money(_nested(item, ("rating", "goodRate", "goodCommentRate", "score"))),
            "sales": _integer(
                _nested(item, ("sales", "salesVolume", "soldCount", "payCount", "monthlySales"))
            ),
            "attributes": _clean_attributes(
                {
                    "category": _nested(item, ("categoryName", "category", "cidName")),
                    "shop_name": _nested(item, ("shopName", "shop_name", "sellerName")),
                }
            ),
        }
    )
    source_id = mapped["source_id"]
    title = mapped["title"]
    price = mapped["price"]
    url = mapped["product_url"]
    if source_id in (None, "") or not str(title or "").strip() or price is None or not url:
        return None
    return {
        "item_id": f"{platform}:{source_id}",
        "source_item_id": str(source_id),
        "platform": platform,
        "title": str(title).strip(),
        "price": price,
        "currency": "CNY",
        "rating": mapped["rating"],
        "sales": mapped["sales"],
        "image_url": mapped["image_url"],
        "product_url": str(url),
        "attributes": mapped["attributes"],
    }
