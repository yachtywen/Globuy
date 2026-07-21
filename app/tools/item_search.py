"""Single-platform product search over the local OpenSearch catalog."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.config import get_settings
from app.infrastructure.opensearch import build_opensearch_client
from app.search.encoder import get_embedding_encoder
from app.search.schemas import ItemSearchOutput, Platform, SearchFilters
from app.search.service import ProductSearchService, SearchNotConfiguredError


@lru_cache(maxsize=1)
def get_product_search_service() -> ProductSearchService:
    settings = get_settings()
    return ProductSearchService(
        build_opensearch_client(settings), get_embedding_encoder(), settings
    )


@tool
async def item_search(
    query: Annotated[str, Field(min_length=1, max_length=500)],
    platform: Platform,
    top_k: Annotated[int, Field(ge=1, le=50)] = 20,
    filters: SearchFilters | None = None,
) -> dict:
    """Search one commerce platform using BM25 and frozen dense-vector retrieval."""

    normalized_query = query.strip()
    try:
        output = await asyncio.to_thread(
            get_product_search_service().search,
            normalized_query,
            platform,
            top_k,
            filters,
        )
    except SearchNotConfiguredError as exc:
        output = ItemSearchOutput(
            status="not_configured", platform=platform, message=str(exc)
        )
    except Exception as exc:
        output = ItemSearchOutput(status="error", platform=platform, message=str(exc))

    return output.model_dump(mode="json")
