"""Deterministic product-image enrichment from the verified local snapshot."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.products.identity import offer_id, product_id

_PLATFORM_ALIASES = {
    "jd": "jingdong",
    "jingdong": "jingdong",
    "taobao": "taobao",
    "tb": "taobao",
    "douyin": "douyin",
}


def _web_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _platform(value: Any, url: str | None = None) -> str | None:
    if isinstance(value, str):
        normalized = _PLATFORM_ALIASES.get(value.strip().casefold())
        if normalized:
            return normalized
    hostname = (urlparse(url).hostname or "").casefold() if url else ""
    if hostname.endswith("jd.com") or hostname.endswith("360buyimg.com"):
        return "jingdong"
    if hostname.endswith("taobao.com") or hostname.endswith("tmall.com"):
        return "taobao"
    if hostname.endswith("jinritemai.com") or hostname.endswith("douyin.com"):
        return "douyin"
    return None


def _identity_keys(item: Mapping[str, Any]) -> set[str]:
    product_url = _web_url(item.get("product_url") or item.get("source_url"))
    platform = _platform(item.get("platform"), product_url)
    source_ids: set[str] = set()
    explicit = item.get("source_item_id")
    if explicit is not None and str(explicit).strip():
        source_ids.add(str(explicit).strip())

    item_id = item.get("item_id") or item.get("id")
    if item_id is not None and str(item_id).strip():
        raw_id = str(item_id).strip()
        if ":" in raw_id:
            raw_platform, source_id = raw_id.split(":", 1)
            platform = _platform(platform or raw_platform, product_url)
            if source_id:
                source_ids.add(source_id)
        else:
            source_ids.add(raw_id)

    keys = {f"url:{product_url}"} if product_url else set()
    if platform:
        keys.update(f"item:{platform}:{source_id}" for source_id in source_ids)
    return keys


@lru_cache(maxsize=8)
def _image_index(path_value: str) -> dict[str, str]:
    path = Path(path_value)
    if not path.is_file():
        return {}
    index: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(item, dict):
                continue
            image_url = _web_url(item.get("image_url"))
            if not image_url:
                continue
            for key in _identity_keys(item):
                index.setdefault(key, image_url)
    return index


def enrich_product_images(
    products: Iterable[Mapping[str, Any]], catalog_path: Path | str | None
) -> list[dict[str, Any]]:
    """Return copied products with stable identities and missing catalog images filled."""

    image_index = _image_index(str(Path(catalog_path).resolve())) if catalog_path else {}
    enriched: list[dict[str, Any]] = []
    for product in products:
        item = dict(product)
        raw_item_id = item.get("item_id") or item.get("id")
        if raw_item_id is not None and str(raw_item_id).strip():
            stable_item_id = str(raw_item_id).strip()
            if not item.get("product_id"):
                item["product_id"] = product_id(stable_item_id)
            if not item.get("offer_id"):
                item["offer_id"] = offer_id(stable_item_id)
        if not _web_url(item.get("image_url")):
            for key in _identity_keys(item):
                image_url = image_index.get(key)
                if image_url:
                    item["image_url"] = image_url
                    break
        enriched.append(item)
    return enriched


def enrich_task_result(
    result: dict[str, Any] | None, catalog_path: Path | str | None
) -> dict[str, Any] | None:
    """Enrich a task-result copy so persisted legacy runs render real images too."""

    if not isinstance(result, dict):
        return result
    enriched = dict(result)
    picks = result.get("picks")
    if isinstance(picks, list):
        records = [item for item in picks if isinstance(item, Mapping)]
        if len(records) == len(picks):
            enriched["picks"] = enrich_product_images(records, catalog_path)
    return enriched


__all__ = ["enrich_product_images", "enrich_task_result"]
