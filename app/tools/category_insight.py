"""CategoryInsight RAG tool backed by a separate OpenSearch card index."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import Field

from app.category.cache import build_category_cache
from app.category.extractor import DeepSeekCategoryExtractor
from app.category.normalization import load_category_aliases
from app.category.reranker import HttpReranker
from app.category.schemas import CategoryInsightOutput
from app.category.service import CategorySearchService
from app.config import get_settings
from app.infrastructure.opensearch import build_opensearch_client
from app.search.encoder import get_embedding_encoder


@lru_cache(maxsize=1)
def get_category_search_service() -> CategorySearchService:
    # Keep this import lazy: app.agent's package initializer imports the tool
    # registry, so importing it while app.tools is being initialized is circular.
    from app.agent.llm import get_chat_model

    settings = get_settings()
    return CategorySearchService(
        build_opensearch_client(settings),
        get_embedding_encoder(),
        DeepSeekCategoryExtractor(get_chat_model()),
        HttpReranker(
            settings.reranker_endpoint,
            timeout_seconds=settings.reranker_timeout_seconds,
        ),
        build_category_cache(
            settings.redis_url,
            timeout_seconds=settings.category_cache_timeout_seconds,
        ),
        load_category_aliases(settings.category_aliases_path),
        settings,
    )


@tool
async def category_insight(
    category: Annotated[str, Field(min_length=1, max_length=200)],
    depth: Literal["quick", "deep"] = "quick",
) -> dict:
    """Return structured category cards; never use model common knowledge as evidence."""

    normalized = category.strip()
    try:
        output, _trace = await get_category_search_service().query_with_trace(
            normalized, depth
        )
    except Exception as exc:
        output = CategoryInsightOutput(
            status="error",
            category=normalized,
            depth=depth,
            confidence=0,
            needs_external_validation=True,
            retrieval_mode="none",
            message=str(exc),
        )
    return output.model_dump(mode="json")
