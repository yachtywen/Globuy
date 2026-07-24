"""Smoke-test the complete live product-search path for all supported platforms.

Run from the repository root after starting FastAPI's dependencies:
    conda run -n globuy python scripts/check_realtime_product_search.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Direct execution puts only ``scripts/`` on sys.path. Add the repository root
# so this check works with the documented ``python scripts/...`` command.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.search.schemas import Platform
from app.tools.item_search import item_search

PLATFORMS: tuple[Platform, ...] = ("taobao", "jingdong", "douyin")


async def check(query: str, top_k: int) -> int:
    settings = get_settings()
    if settings.realtime_product_provider != "justone" or settings.justone_api_token is None:
        print("实时商品 Provider 未配置：请检查 GLOBUY_REALTIME_PRODUCT_PROVIDER 和 GLOBUY_JUSTONE_API_TOKEN。")
        return 2

    failed = False
    for platform in PLATFORMS:
        result = await item_search.ainvoke(
            {"query": query, "platform": platform, "top_k": top_k}
        )
        candidates = result.get("candidates", [])
        status = result.get("status")
        source = result.get("source_kind")
        print(
            f"{platform}: status={status}, candidates={len(candidates)}, "
            f"source={source}, cache_hit={result.get('cache_hit', False)}"
        )
        if result.get("message"):
            print(f"  {result['message']}")
        if status != "ok" or not candidates:
            failed = True

    if failed:
        print("至少一个平台未返回可展示商品；请保留以上状态和后端日志用于排查。")
        return 1
    print("三平台实时商品搜索通过。")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Taobao, JD, and Douyin live search")
    parser.add_argument("--query", default="耳机", help="search keyword")
    parser.add_argument("--top-k", type=int, default=5, choices=range(1, 21))
    args = parser.parse_args()
    raise SystemExit(asyncio.run(check(args.query, args.top_k)))


if __name__ == "__main__":
    main()
