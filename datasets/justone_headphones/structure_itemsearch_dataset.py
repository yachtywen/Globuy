"""Build the compact ItemSearch candidate dataset from the collected snapshot."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

SOURCE_PATH = Path("datasets/headphones_1000/normalized/headphones.jsonl")
OUTPUT_DIR = Path("datasets/headphones_1000/structured")

PLATFORM_MAP = {"taobao": "taobao", "jd": "jingdong", "douyin": "douyin"}
OUTPUT_FIELDS = (
    "item_id",
    "platform",
    "title",
    "price",
    "currency",
    "rating",
    "sales",
    "image_url",
    "attributes",
    "product_url",
)


def _not_empty(value: Any) -> bool:
    return value not in (None, "", [], {})


def _clean_mapping(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if _not_empty(value)}


def _structured_attributes(platform: str, source: dict[str, Any]) -> dict[str, Any]:
    attributes = source.get("attributes") or {}
    if not isinstance(attributes, dict):
        return {}

    if platform == "taobao":
        return _clean_mapping(
            {
                "spu_id": attributes.get("spu_id"),
                "shop_id": attributes.get("shop_id"),
                "shop_name": attributes.get("shop_name"),
                "seller_location": attributes.get("seller_location"),
                "item_type": attributes.get("item_type"),
                "tags": attributes.get("tags"),
            }
        )
    if platform == "jd":
        category_ids = [
            value
            for value in (
                attributes.get("category_1"),
                attributes.get("category_2"),
                attributes.get("category_3"),
            )
            if _not_empty(value)
        ]
        return _clean_mapping(
            {
                "shop_id": attributes.get("shop_id"),
                "shop_name": attributes.get("shop_name"),
                "vendor_id": attributes.get("vendor_id"),
                "category_ids": category_ids,
                "is_self_operated": attributes.get("is_self_operated"),
                "slogan": attributes.get("slogan"),
            }
        )
    if platform == "douyin":
        return _clean_mapping(
            {
                "shop_id": attributes.get("shop_id"),
                "shop_name": attributes.get("shop_name"),
                "shop_score": attributes.get("shop_score"),
                "category_path": attributes.get("category_path"),
                "category_ids": attributes.get("category_ids"),
                "tags": attributes.get("tag_codes"),
                "rating_type": attributes.get("rating_type"),
            }
        )
    raise ValueError(f"Unsupported source platform: {platform}")


def structure_candidate(source: dict[str, Any]) -> dict[str, Any]:
    source_platform = str(source.get("platform") or "")
    platform = PLATFORM_MAP.get(source_platform)
    if platform is None:
        raise ValueError(f"Unsupported source platform: {source_platform}")

    source_id = str(source.get("source_item_id") or "").strip()
    title = str(source.get("title") or "").strip()
    price = source.get("price")
    if not source_id or not title or price is None:
        raise ValueError("Candidate is missing item ID, title, or price")

    rating = source.get("rating")
    sales = source.get("sales")
    return {
        "item_id": f"{platform}:{source_id}",
        "platform": platform,
        "title": title,
        "price": float(price),
        "currency": str(source.get("currency") or "CNY"),
        "rating": float(rating) if rating is not None else None,
        "sales": int(sales) if sales is not None else None,
        "image_url": str(source["image_url"]) if source.get("image_url") else None,
        "attributes": _structured_attributes(source_platform, source),
        "product_url": str(source["source_url"]) if source.get("source_url") else None,
    }


def _coverage(items: list[dict[str, Any]]) -> dict[str, float]:
    if not items:
        return {field: 0.0 for field in OUTPUT_FIELDS}
    return {
        field: round(
            sum(_not_empty(item.get(field)) for item in items) / len(items), 4
        )
        for field in OUTPUT_FIELDS
    }


def build_dataset(
    source_path: Path = SOURCE_PATH, output_dir: Path = OUTPUT_DIR
) -> dict[str, Any]:
    source_rows = [
        json.loads(line)
        for line in source_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    candidates = [structure_candidate(source) for source in source_rows]
    item_ids = [item["item_id"] for item in candidates]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("Structured candidates contain duplicate item_id values")

    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "itemsearch_candidates.jsonl"
    jsonl_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in candidates),
        encoding="utf-8",
    )
    csv_path = output_dir / "itemsearch_candidates.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for item in candidates:
            row = dict(item)
            row["attributes"] = json.dumps(row["attributes"], ensure_ascii=False)
            writer.writerow(row)

    platform_counts = Counter(item["platform"] for item in candidates)
    schema = {
        "schema_version": "itemsearch-candidate-v2",
        "fields": {
            "item_id": "str",
            "platform": "taobao | jingdong | douyin",
            "title": "str",
            "price": "float",
            "currency": "str",
            "rating": "float | null",
            "sales": "int | null",
            "image_url": "str | null",
            "attributes": "dict",
            "product_url": "str | null",
        },
        "dropped_source_fields": [
            "source_item_id",
            "captured_at",
            "detail_enriched",
            "source_keyword",
            "source_page",
        ],
        "image_policy": "Only image_url is retained; raw image objects are excluded.",
    }
    quality = {
        "source_records": len(source_rows),
        "structured_records": len(candidates),
        "duplicate_item_ids": len(item_ids) - len(set(item_ids)),
        "platform_counts": dict(sorted(platform_counts.items())),
        "coverage": _coverage(candidates),
        "platform_values": sorted(platform_counts),
    }
    (output_dir / "schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "quality_report.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return quality


if __name__ == "__main__":
    print(json.dumps(build_dataset(), ensure_ascii=False, indent=2))
