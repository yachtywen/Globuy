"""Run a redacted, real JustOne provider smoke test."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json

from app.config import get_settings
from app.products.providers.base import ProviderSearchRequest
from app.products.providers.justone import JustOneProvider
from app.search.schemas import SearchFilters


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("keyword")
    parser.add_argument("--max-price", type=float, default=None)
    args = parser.parse_args()
    provider = JustOneProvider(get_settings())
    try:
        requests = [
            ProviderSearchRequest(
                provider="justone",
                platform=platform,
                keyword=args.keyword,
                filters=SearchFilters(max_price=args.max_price),
                request_key=hashlib.sha256(
                    f"{platform}:{args.keyword}:{args.max_price}".encode()
                ).hexdigest(),
            )
            for platform in ("taobao", "jingdong", "douyin")
        ]
        pages = await asyncio.gather(*(provider.search(request) for request in requests))
    finally:
        await provider.aclose()
    print(
        json.dumps(
            [
                {
                    "platform": page.platform,
                    "status": page.status,
                    "count": len(page.items),
                    "has_more": page.has_more,
                    "duration_ms": page.duration_ms,
                    "business_code": page.business_code,
                    "request_id": page.request_id,
                    "message": page.message,
                }
                for page in pages
            ],
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
