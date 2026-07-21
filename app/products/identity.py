"""Stable identifiers shared by MySQL and OpenSearch projections."""

from __future__ import annotations

import hashlib


def stable_id(kind: str, value: str) -> str:
    return hashlib.sha256(f"globuy:{kind}:{value}".encode()).hexdigest()[:32]


def source_item_id(item_id: str, platform: str) -> str:
    prefix = f"{platform}:"
    return item_id[len(prefix) :] if item_id.startswith(prefix) else item_id


def product_id(item_id: str) -> str:
    return stable_id("product", item_id)


def offer_id(item_id: str) -> str:
    return stable_id("offer", item_id)
