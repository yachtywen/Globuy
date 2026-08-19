"""CLI for non-destructive product catalog lifecycle operations."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.config import get_settings
from app.database.session import Database
from app.infrastructure.opensearch import build_opensearch_client
from app.products.catalog.lifecycle import CatalogLifecycleService
from app.search.encoder import get_embedding_encoder
from app.search.service import ProductIndexManager


async def _main(action: str, value: str | None) -> None:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("GLOBUY_DATABASE_URL is required")
    database = Database(settings.database_url.get_secret_value())
    service = CatalogLifecycleService(
        database,
        ProductIndexManager(build_opensearch_client(settings), get_embedding_encoder(), settings),
        settings,
    )
    try:
        if action == "stats":
            result = await service.stats()
        elif action == "audit":
            result = await service.migration_audit()
        elif action == "cleanup":
            result = await service.cleanup_scope_members()
        elif action == "expire":
            result = {
                "expired_offers": await service.expire_offers(stale_seconds=int(value or "2592000"))
            }
        elif action == "rebuild":
            if not value:
                raise ValueError("rebuild requires --value physical-index")
            result = await service.rebuild(value)
        else:
            raise ValueError(action)
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["stats", "audit", "cleanup", "expire", "rebuild"])
    parser.add_argument("--value")
    args = parser.parse_args()
    asyncio.run(_main(args.action, args.value))


if __name__ == "__main__":
    main()
