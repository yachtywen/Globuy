"""Online CategoryInsight recall, rerank, extraction, cache, and degradation."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from statistics import fmean
from typing import Any

from opensearchpy import OpenSearch

from app.category.cache import CategoryCache, category_cache_key
from app.category.extractor import (
    CategoryExtractionError,
    CategoryExtractionNotConfigured,
    InsightExtractor,
)
from app.category.normalization import CategoryAliases
from app.category.opensearch import (
    category_bm25_body,
    category_hybrid_body,
    category_semantic_body,
    select_retrieval_profile,
)
from app.category.reranker import Reranker, rerank_cards
from app.category.schemas import CategoryCard, CategoryInsightOutput
from app.config import Settings, get_settings
from app.search.encoder import EmbeddingEncoder


@dataclass(slots=True)
class CategoryQueryTrace:
    canonical_category: str = ""
    depth: str = "quick"
    cache_hit: bool = False
    retrieval_profile: str = ""
    coarse_count: int = 0
    rerank_used: bool = False
    final_count: int = 0
    cards_by_type: dict[str, int] = field(default_factory=dict)
    index_build_id: str = ""
    data_as_of: str | None = None
    degraded_reason: str | None = None


class CategorySearchNotConfiguredError(RuntimeError):
    pass


def _mapping_metadata(mapping: dict[str, Any]) -> dict[str, Any] | None:
    for value in mapping.values():
        meta = value.get("mappings", {}).get("_meta")
        if isinstance(meta, dict):
            return meta
    return None


def _empty_output(
    *,
    status: str,
    category: str,
    depth: str,
    retrieval_mode: str,
    message: str,
) -> CategoryInsightOutput:
    return CategoryInsightOutput(
        status=status,
        category=category,
        depth=depth,
        confidence=0,
        needs_external_validation=True,
        retrieval_mode=retrieval_mode,
        message=message,
    )


def _card_from_hit(hit: dict[str, Any]) -> CategoryCard:
    source = hit["_source"]
    fields = CategoryCard.model_fields
    return CategoryCard.model_validate({key: source[key] for key in fields})


class CategorySearchService:
    def __init__(
        self,
        client: OpenSearch,
        encoder: EmbeddingEncoder,
        extractor: InsightExtractor,
        reranker: Reranker,
        cache: CategoryCache,
        aliases: CategoryAliases,
        settings: Settings | None = None,
    ) -> None:
        self.client = client
        self.encoder = encoder
        self.extractor = extractor
        self.reranker = reranker
        self.cache = cache
        self.aliases = aliases
        self.settings = settings or get_settings()
        self._index_meta: dict[str, Any] | None = None

    def _ensure_ready(self) -> dict[str, Any]:
        if self._index_meta is not None:
            return self._index_meta
        try:
            if not self.client.ping():
                raise CategorySearchNotConfiguredError("OpenSearch 当前不可用")
            alias = self.settings.opensearch_category_alias
            if not self.client.indices.exists(index=alias):
                raise CategorySearchNotConfiguredError("CategoryInsight 索引尚未构建")
            meta = _mapping_metadata(self.client.indices.get_mapping(index=alias))
        except CategorySearchNotConfiguredError:
            raise
        except Exception as exc:
            raise CategorySearchNotConfiguredError(
                f"无法检查 CategoryInsight 索引: {exc}"
            ) from exc
        if not meta:
            raise CategorySearchNotConfiguredError("CategoryInsight 索引缺少 _meta")
        expected = {
            "embedding_model": self.settings.embedding_model_name,
            "embedding_dimensions": self.settings.embedding_dimensions,
            "embedding_normalized": True,
            "semantic_text_version": "category-card-v1",
            "card_schema_version": "category-card-v1",
        }
        if any(meta.get(key) != value for key, value in expected.items()):
            raise CategorySearchNotConfiguredError("CategoryInsight 索引元数据不兼容")
        self._index_meta = meta
        return meta

    def _pipeline_for(self, profile: str) -> str:
        return {
            "exact": self.settings.opensearch_category_pipeline_exact,
            "balanced": self.settings.opensearch_category_pipeline_balanced,
            "semantic": self.settings.opensearch_category_pipeline_semantic,
        }[profile]

    async def _search(
        self,
        *,
        query: str,
        category: str,
        depth: str,
        profile: str,
        vector: list[float] | None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] | None = None
        if vector is None:
            body = category_bm25_body(
                query,
                category_key=category,
                depth=depth,
                coarse_k=self.settings.category_coarse_k,
            )
        elif profile == "semantic_only":
            body = category_semantic_body(
                vector,
                category_key=category,
                depth=depth,
                coarse_k=self.settings.category_coarse_k,
            )
        else:
            body = category_hybrid_body(
                query,
                vector,
                category_key=category,
                depth=depth,
                coarse_k=self.settings.category_coarse_k,
            )
            params = {"search_pipeline": self._pipeline_for(profile)}
        search_kwargs: dict[str, Any] = {
            "index": self.settings.opensearch_category_alias,
            "body": body,
        }
        if params is not None:
            search_kwargs["params"] = params
        response = await asyncio.to_thread(self.client.search, **search_kwargs)
        return response.get("hits", {}).get("hits", [])

    async def query_with_trace(
        self, query: str, depth: str = "quick"
    ) -> tuple[CategoryInsightOutput, CategoryQueryTrace]:
        normalized_query = query.strip()
        canonical = self.aliases.normalize(normalized_query)
        trace = CategoryQueryTrace(canonical_category=canonical or "", depth=depth)
        if depth not in {"quick", "deep"}:
            return (
                _empty_output(
                    status="error",
                    category=canonical or normalized_query,
                    depth="quick",
                    retrieval_mode="none",
                    message="depth 必须是 quick 或 deep",
                ),
                trace,
            )
        if canonical is None:
            return (
                _empty_output(
                    status="insufficient_data",
                    category=normalized_query,
                    depth=depth,
                    retrieval_mode="none",
                    message="当前知识库没有该品类卡片，可由主 Agent 尝试 WebSearch。",
                ),
                trace,
            )

        try:
            meta = await asyncio.to_thread(self._ensure_ready)
        except CategorySearchNotConfiguredError as exc:
            return (
                _empty_output(
                    status="not_configured",
                    category=canonical,
                    depth=depth,
                    retrieval_mode="none",
                    message=str(exc),
                ),
                trace,
            )
        trace.index_build_id = str(meta.get("build_id") or "unknown")
        key = category_cache_key(
            category=canonical,
            query=normalized_query,
            depth=depth,
            index_build_id=trace.index_build_id,
            prompt_version=self.extractor.prompt_version,
        )
        cached = await self.cache.get(key)
        if cached is not None:
            trace.cache_hit = True
            trace.data_as_of = cached.data_as_of.isoformat() if cached.data_as_of else None
            return cached, trace

        profile = select_retrieval_profile(normalized_query, canonical)
        trace.retrieval_profile = profile
        degraded_reason: str | None = None
        vector: list[float] | None = None
        try:
            vector = await asyncio.to_thread(self.encoder.encode_query, normalized_query)
            resolved_revision = self.encoder.metadata.revision
            if resolved_revision != meta.get("embedding_revision"):
                vector = None
                degraded_reason = "embedding_revision_mismatch"
        except Exception:
            vector = None
            degraded_reason = "embedding_unavailable"

        try:
            hits = await self._search(
                query=normalized_query,
                category=canonical,
                depth=depth,
                profile=profile,
                vector=vector,
            )
        except Exception as exc:
            return (
                _empty_output(
                    status="error",
                    category=canonical,
                    depth=depth,
                    retrieval_mode="bm25" if vector is None else profile,
                    message=f"CategoryInsight 检索失败: {exc}",
                ),
                trace,
            )
        trace.coarse_count = len(hits)
        if not hits:
            return (
                _empty_output(
                    status="insufficient_data",
                    category=canonical,
                    depth=depth,
                    retrieval_mode="bm25" if vector is None else profile,
                    message="当前品类没有召回到可用卡片，可由主 Agent 尝试 WebSearch。",
                ),
                trace,
            )
        try:
            cards = [_card_from_hit(hit) for hit in hits]
        except Exception as exc:
            return (
                _empty_output(
                    status="error",
                    category=canonical,
                    depth=depth,
                    retrieval_mode=profile,
                    message=f"CategoryCard 索引数据不符合 Schema: {exc}",
                ),
                trace,
            )

        top_k = (
            self.settings.category_quick_k
            if depth == "quick"
            else self.settings.category_deep_k
        )
        threshold = self.settings.category_rerank_bypass_score
        bypass_high_score = bool(
            threshold is not None
            and hits
            and hits[0].get("_score") is not None
            and float(hits[0]["_score"]) >= threshold
        )
        if len(cards) > top_k and not bypass_high_score:
            try:
                cards = await rerank_cards(
                    self.reranker,
                    query=normalized_query,
                    cards=cards,
                    top_k=top_k,
                )
                trace.rerank_used = True
            except Exception as exc:
                if self.settings.category_reranker_required:
                    return (
                        _empty_output(
                            status="not_configured",
                            category=canonical,
                            depth=depth,
                            retrieval_mode="hybrid_without_rerank",
                            message=str(exc),
                        ),
                        trace,
                    )
                cards = cards[:top_k]
                degraded_reason = degraded_reason or "reranker_unavailable"
        else:
            cards = cards[:top_k]

        trace.final_count = len(cards)
        trace.cards_by_type = dict(sorted(Counter(card.card_type for card in cards).items()))
        try:
            extracted = await self.extractor.extract_insight(normalized_query, depth, cards)
        except CategoryExtractionNotConfigured as exc:
            return (
                _empty_output(
                    status="not_configured",
                    category=canonical,
                    depth=depth,
                    retrieval_mode=profile,
                    message=str(exc),
                ),
                trace,
            )
        except CategoryExtractionError as exc:
            return (
                _empty_output(
                    status="error",
                    category=canonical,
                    depth=depth,
                    retrieval_mode=profile,
                    message=str(exc),
                ),
                trace,
            )

        confidence = round(fmean(card.confidence for card in cards), 6)
        data_as_of = min(card.last_updated for card in cards)
        status = "partial" if degraded_reason else "ok"
        retrieval_mode = "bm25" if vector is None else profile
        if degraded_reason == "reranker_unavailable":
            retrieval_mode = "hybrid_without_rerank"
        output = CategoryInsightOutput(
            status=status,
            category=canonical,
            depth=depth,
            components=extracted.components,
            bestsellers=extracted.bestsellers,
            attributes=extracted.attributes if depth == "deep" else [],
            price_tiers=extracted.price_tiers,
            confidence=confidence,
            needs_external_validation=confidence < 0.5 or status != "ok",
            data_as_of=data_as_of,
            retrieval_mode=retrieval_mode,
            cache_hit=False,
            message="基于已发布的离线 CategoryCard 快照。",
        )
        trace.data_as_of = data_as_of.isoformat()
        trace.degraded_reason = degraded_reason
        if output.status == "ok":
            await self.cache.set(key, output, self.settings.category_cache_ttl_seconds)
        return output, trace

    async def query(self, query: str, depth: str = "quick") -> CategoryInsightOutput:
        output, _ = await self.query_with_trace(query, depth)
        return output
