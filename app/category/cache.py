"""Optional Redis query cache for validated CategoryInsight outputs."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Protocol

from app.category.schemas import CategoryInsightOutput


class CategoryCache(Protocol):
    async def get(self, key: str) -> CategoryInsightOutput | None: ...

    async def set(self, key: str, value: CategoryInsightOutput, ttl: int) -> bool: ...


class NullCategoryCache:
    async def get(self, key: str) -> CategoryInsightOutput | None:
        return None

    async def set(self, key: str, value: CategoryInsightOutput, ttl: int) -> bool:
        return False


class RedisCategoryCache:
    def __init__(self, client, *, timeout_seconds: float = 0.25) -> None:
        self.client = client
        self.timeout_seconds = timeout_seconds

    async def get(self, key: str) -> CategoryInsightOutput | None:
        try:
            payload = await asyncio.wait_for(
                self.client.get(key), timeout=self.timeout_seconds
            )
            if not payload:
                return None
            output = CategoryInsightOutput.model_validate_json(payload)
            return output.model_copy(update={"cache_hit": True})
        except Exception:
            return None

    async def set(self, key: str, value: CategoryInsightOutput, ttl: int) -> bool:
        try:
            await asyncio.wait_for(
                self.client.setex(key, ttl, value.model_dump_json()),
                timeout=self.timeout_seconds,
            )
            return True
        except Exception:
            return False


def build_category_cache(redis_url: str | None, *, timeout_seconds: float) -> CategoryCache:
    if not redis_url:
        return NullCategoryCache()
    try:
        from redis.asyncio import from_url
    except ImportError:
        return NullCategoryCache()
    return RedisCategoryCache(
        from_url(redis_url, decode_responses=True), timeout_seconds=timeout_seconds
    )


def category_cache_key(
    *,
    category: str,
    query: str,
    depth: str,
    index_build_id: str,
    prompt_version: str,
) -> str:
    query_hash = hashlib.sha256(query.strip().encode("utf-8")).hexdigest()[:20]
    return f"cinsight:{category}:{query_hash}:{depth}:{index_build_id}:{prompt_version}"
