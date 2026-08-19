"""OpenSearch mappings, RRF pipeline and hybrid-query builders."""

from __future__ import annotations

from typing import Any

from app.search.documents import attribute_term
from app.search.encoder import EmbeddingMetadata
from app.search.schemas import Platform, SearchFilters


def search_pipeline_body() -> dict[str, Any]:
    return {
        "description": "globuy product BM25 + dense-vector RRF",
        "phase_results_processors": [
            {"score-ranker-processor": {"combination": {"technique": "rrf"}}}
        ],
    }


def product_index_body(metadata: EmbeddingMetadata) -> dict[str, Any]:
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_shards": 1,
                "number_of_replicas": 0,
            }
        },
        "mappings": {
            "_meta": {
                "embedding_model": metadata.model_id,
                "embedding_revision": metadata.revision,
                "embedding_dimensions": metadata.dimensions,
                "embedding_normalized": metadata.normalized,
                "semantic_text_version": metadata.semantic_text_version,
            },
            "properties": {
                "item_id": {"type": "keyword"},
                "product_id": {"type": "keyword"},
                "offer_id": {"type": "keyword"},
                "category_key": {"type": "keyword"},
                "category_path": {"type": "keyword"},
                "captured_at": {"type": "date"},
                "last_seen_at": {"type": "date"},
                "is_active": {"type": "boolean"},
                "semantic_hash": {"type": "keyword"},
                "projection_hash": {"type": "keyword"},
                "platform": {"type": "keyword"},
                "title": {"type": "text", "analyzer": "cjk"},
                "price": {"type": "double"},
                "currency": {"type": "keyword"},
                "rating": {"type": "float"},
                "sales": {"type": "long"},
                "image_url": {"type": "keyword", "index": False},
                "attributes": {"type": "object", "enabled": False},
                "attribute_terms": {"type": "keyword"},
                "product_url": {"type": "keyword", "index": False},
                "semantic_text": {"type": "text", "index": False},
                "content_vector": {
                    "type": "knn_vector",
                    "dimension": metadata.dimensions,
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                    },
                },
            },
        },
    }


def _post_filters(filters: SearchFilters | None) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    if filters is None:
        return clauses
    price_range: dict[str, float] = {}
    if filters.min_price is not None:
        price_range["gte"] = filters.min_price
    if filters.max_price is not None:
        price_range["lte"] = filters.max_price
    if price_range:
        clauses.append({"range": {"price": price_range}})
    if filters.currency:
        clauses.append({"term": {"currency": filters.currency.upper()}})
    if filters.min_rating is not None:
        clauses.append({"range": {"rating": {"gte": filters.min_rating}}})
    if filters.min_sales is not None:
        clauses.append({"range": {"sales": {"gte": filters.min_sales}}})
    for key, value in sorted((filters.attribute_equals or {}).items()):
        clauses.append({"term": {"attribute_terms": attribute_term(key, value)}})
    return clauses


def hybrid_search_body(
    query: str,
    vector: list[float],
    platform: Platform,
    pool_size: int,
    filters: SearchFilters | None = None,
    *,
    category_key: str | None = None,
    offer_ids: list[str] | None = None,
    fresh_after: str | None = None,
) -> dict[str, Any]:
    recall_filters: list[dict[str, Any]] = [
        {"term": {"platform": platform}},
        {"term": {"is_active": True}},
    ]
    if category_key:
        recall_filters.append({"term": {"category_key": category_key}})
    if offer_ids:
        if len(offer_ids) > 120:
            raise ValueError("offer_ids 最多允许 120 个")
        recall_filters.append({"terms": {"offer_id": offer_ids}})
    if fresh_after:
        recall_filters.append({"range": {"last_seen_at": {"gte": fresh_after}}})
    body: dict[str, Any] = {
        "size": pool_size,
        "_source": {"excludes": ["content_vector", "semantic_text", "attribute_terms"]},
        "query": {
            "hybrid": {
                "queries": [
                    {"match": {"title": {"query": query}}},
                    {
                        "knn": {
                            "content_vector": {
                                "vector": vector,
                                "k": pool_size,
                            }
                        }
                    },
                ],
                # Platform defines the single-platform recall domain and therefore
                # constrains both BM25 and KNN before RRF fusion.
                "filter": {"bool": {"filter": recall_filters}},
            }
        },
    }
    post_filters = _post_filters(filters)
    if post_filters:
        # Offer constraints must not change either recall route. OpenSearch applies
        # post_filter after hybrid scoring/RRF; the service deliberately requests a
        # larger pool before taking the public top_k.
        body["post_filter"] = {"bool": {"filter": post_filters}}
    return body
