"""OpenSearch mapping, pipelines, and query builders for CategoryCard RAG."""

from __future__ import annotations

from typing import Any, Literal

from app.search.encoder import EmbeddingMetadata

type RetrievalProfile = Literal["exact", "balanced", "semantic", "semantic_only"]

SEMANTIC_TOKENS = frozenset(
    {"气质", "感觉", "风格", "氛围", "适合", "送礼", "高级感", "简约", "通勤"}
)
CONSTRAINT_TOKENS = frozenset(
    {"蓝牙", "无线", "有线", "降噪", "头戴", "入耳", "开放式", "骨传导", "游戏", "运动"}
)
COLLOQUIAL_TOKENS = frozenset({"想要", "想买", "推荐", "送", "适合", "有没有", "哪个好"})


def category_embedding_metadata(base: EmbeddingMetadata) -> EmbeddingMetadata:
    return EmbeddingMetadata(
        model_id=base.model_id,
        revision=base.revision,
        dimensions=base.dimensions,
        normalized=base.normalized,
        semantic_text_version="category-card-v1",
    )


def category_pipeline_body(weights: tuple[float, float]) -> dict[str, Any]:
    return {
        "description": f"globuy category KNN/BM25 min-max fusion {weights}",
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {
                        "technique": "arithmetic_mean",
                        "parameters": {"weights": list(weights)},
                    },
                }
            }
        ],
    }


def category_index_body(
    metadata: EmbeddingMetadata,
    *,
    aliases_version: str,
    source_sha256: str,
    build_id: str,
    pipeline_names: dict[str, str],
) -> dict[str, Any]:
    category_meta = category_embedding_metadata(metadata)
    return {
        "settings": {
            "index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}
        },
        "mappings": {
            "_meta": {
                "embedding_model": category_meta.model_id,
                "embedding_revision": category_meta.revision,
                "embedding_dimensions": category_meta.dimensions,
                "embedding_normalized": category_meta.normalized,
                "semantic_text_version": category_meta.semantic_text_version,
                "card_schema_version": "category-card-v1",
                "normalizer_version": aliases_version,
                "source_sha256": source_sha256,
                "build_id": build_id,
                "pipeline_names": pipeline_names,
            },
            "properties": {
                "card_id": {"type": "keyword"},
                "category_key": {"type": "keyword"},
                "category": {"type": "text", "analyzer": "cjk"},
                "card_type": {"type": "keyword"},
                "summary": {"type": "text", "analyzer": "cjk"},
                "raw_evidence": {"type": "text", "index": False},
                "last_updated": {"type": "date"},
                "confidence": {"type": "float"},
                "semantic_text": {"type": "text", "index": False},
                "content_vector": {
                    "type": "knn_vector",
                    "dimension": category_meta.dimensions,
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                    },
                },
            },
        },
    }


def select_retrieval_profile(query: str, canonical_category: str) -> RetrievalProfile:
    normalized = "".join(query.lower().split())
    remainder = normalized.replace("".join(canonical_category.lower().split()), "")
    if not remainder:
        return "exact"
    if any(token in normalized for token in SEMANTIC_TOKENS):
        if any(token in normalized for token in CONSTRAINT_TOKENS):
            return "semantic"
        if any(token in normalized for token in COLLOQUIAL_TOKENS):
            return "semantic_only"
        return "semantic"
    if any(token in normalized for token in CONSTRAINT_TOKENS):
        return "balanced"
    return "exact"


def _filters(category_key: str, depth: str) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = [{"term": {"category_key": category_key}}]
    if depth == "quick":
        clauses.append({"terms": {"card_type": ["bestseller", "price_range"]}})
    return clauses


def category_hybrid_body(
    query: str,
    vector: list[float],
    *,
    category_key: str,
    depth: str,
    coarse_k: int,
) -> dict[str, Any]:
    return {
        "size": coarse_k,
        "_source": {"excludes": ["content_vector", "semantic_text"]},
        "query": {
            "hybrid": {
                "queries": [
                    {"knn": {"content_vector": {"vector": vector, "k": coarse_k}}},
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["category^2", "summary"],
                            "analyzer": "cjk",
                        }
                    },
                ],
                "filter": {"bool": {"filter": _filters(category_key, depth)}},
            }
        },
    }


def category_semantic_body(
    vector: list[float], *, category_key: str, depth: str, coarse_k: int
) -> dict[str, Any]:
    return {
        "size": coarse_k,
        "_source": {"excludes": ["content_vector", "semantic_text"]},
        "query": {
            "knn": {
                "content_vector": {
                    "vector": vector,
                    "k": coarse_k,
                    "filter": {"bool": {"filter": _filters(category_key, depth)}},
                }
            }
        },
    }


def category_bm25_body(
    query: str, *, category_key: str, depth: str, coarse_k: int
) -> dict[str, Any]:
    return {
        "size": coarse_k,
        "_source": {"excludes": ["content_vector", "semantic_text"]},
        "query": {
            "bool": {
                "filter": _filters(category_key, depth),
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["category^2", "summary"],
                            "analyzer": "cjk",
                        }
                    }
                ],
            }
        },
    }
