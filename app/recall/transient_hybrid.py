"""Deterministic BM25 + transient dense-vector selection for product groups."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

import faiss
import numpy as np

from app.products.grouping import CandidateGroup
from app.search.candidate_encoder import CandidateEmbeddingEncoder
from app.search.schemas import Platform

SEMANTIC_TEXT_VERSION = "candidate-group-v1"
_VOLATILE_ATTRIBUTE_PARTS = (
    "price",
    "rating",
    "sales",
    "stock",
    "inventory",
    "discount",
    "coupon",
    "promotion",
    "shipping",
    "shop",
    "seller",
    "tag",
)


@dataclass(frozen=True)
class FaissSelection:
    groups: list[CandidateGroup]
    method: str
    embedding_model: str | None = None
    embedding_revision: str | None = None
    embedding_cache_hits: int = 0
    embedding_cache_misses: int = 0
    embedding_duration_ms: int = 0
    bm25_duration_ms: int = 0
    faiss_duration_ms: int = 0


class TransientFaissFlatIndex:
    """Exact cosine search over one request's normalized candidate vectors."""

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.index = faiss.IndexFlatIP(dimensions)

    def add(self, vectors: Any) -> None:
        values = self._vectors(vectors)
        self.index.add(values)

    def search(self, query: Any, limit: int) -> list[tuple[int, float]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        scores, positions = self.index.search(self._vectors(query), limit)
        return [
            (int(position), float(score))
            for position, score in zip(positions[0], scores[0], strict=True)
            if position >= 0
        ]

    def _vectors(self, values: Any) -> np.ndarray:
        vectors = np.asarray(values, dtype="float32")
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        if vectors.ndim != 2 or vectors.shape[1] != self.dimensions:
            raise ValueError(f"transient vectors must have shape (*, {self.dimensions})")
        if not np.isfinite(vectors).all():
            raise ValueError("transient vectors must be finite")
        vectors = np.ascontiguousarray(vectors)
        faiss.normalize_L2(vectors)
        return vectors


def tokenize_bm25(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens = re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", normalized)
    for sequence in re.findall(r"[\u4e00-\u9fff]+", normalized):
        tokens.extend(sequence)
        tokens.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return tokens


def _stable_attributes(attributes: dict[str, Any]) -> list[tuple[str, str]]:
    stable: list[tuple[str, str]] = []
    for raw_key, raw_value in sorted(attributes.items(), key=lambda pair: str(pair[0])):
        key = unicodedata.normalize("NFKC", str(raw_key)).casefold().strip()
        if not key or any(part in key for part in _VOLATILE_ATTRIBUTE_PARTS):
            continue
        if isinstance(raw_value, (str, int, float, bool)):
            value = str(raw_value).strip()
        elif isinstance(raw_value, list) and len(raw_value) <= 8:
            value = " ".join(str(item).strip() for item in raw_value if str(item).strip())
        else:
            continue
        if value:
            stable.append((key, value[:256]))
    return stable[:24]


def group_search_text(group: CandidateGroup) -> str:
    item = group.representative
    attributes = _stable_attributes(item.attributes)
    return " ".join(
        [item.title, *(f"{key} {value}" for key, value in attributes)]
    ).strip()


def semantic_cache_key(group: CandidateGroup, encoder: CandidateEmbeddingEncoder) -> str:
    metadata = encoder.metadata
    semantic_text = group_search_text(group)
    digest = hashlib.sha256(semantic_text.encode("utf-8")).hexdigest()
    return ":".join(
        [metadata.model_id, metadata.revision, SEMANTIC_TEXT_VERSION, digest]
    )


def bm25_order(
    groups: list[CandidateGroup], query: str, *, k1: float = 1.2, b: float = 0.75
) -> list[CandidateGroup]:
    documents = [tokenize_bm25(group_search_text(group)) for group in groups]
    query_terms = tokenize_bm25(query)
    document_frequency: Counter[str] = Counter()
    for document in documents:
        document_frequency.update(set(document))
    average_length = sum(map(len, documents)) / len(documents) if documents else 0.0
    scores: list[float] = []
    for document in documents:
        frequencies = Counter(document)
        score = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            df = document_frequency[term]
            idf = math.log(1 + (len(documents) - df + 0.5) / (df + 0.5))
            normalization = frequency + k1 * (
                1 - b + b * len(document) / average_length
            ) if average_length else frequency + k1
            score += idf * frequency * (k1 + 1) / normalization
        scores.append(score)
    return [
        groups[index]
        for index in sorted(
            range(len(groups)),
            key=lambda index: (
                -scores[index],
                _source_rank(groups[index]),
                groups[index].input_order,
            ),
        )
    ]


def _source_rank(group: CandidateGroup) -> int:
    return min(
        (
            offer.source_rank or offer.retrieval_rank
            for offer in group.offers
            if offer.source_rank is not None or offer.retrieval_rank is not None
        ),
        default=10**9,
    )


def _balanced(groups: list[CandidateGroup], limit: int) -> list[CandidateGroup]:
    buckets: dict[Platform, list[CandidateGroup]] = defaultdict(list)
    for group in groups:
        buckets[group.representative.platform].append(group)
    active_platforms = [
        platform for platform in ("taobao", "jingdong", "douyin") if buckets[platform]
    ]
    if not active_platforms:
        return []
    # Protect a minimum cross-platform floor, then return to the global ranking.
    # Strict equal quotas can discard highly relevant global top results merely
    # because their platform already filled one third of the shortlist.
    floor = max(1, limit // (2 * len(active_platforms)))
    selected: list[CandidateGroup] = []
    selected_ids: set[str] = set()
    for _ in range(floor):
        for platform in active_platforms:
            if buckets[platform] and len(selected) < limit:
                group = buckets[platform].pop(0)
                selected.append(group)
                selected_ids.add(group.product_group_id)
    for group in groups:
        if len(selected) >= limit:
            break
        if group.product_group_id not in selected_ids:
            selected.append(group)
            selected_ids.add(group.product_group_id)
    return selected


def select_bm25_groups(
    groups: list[CandidateGroup], lexical_query: str, limit: int
) -> FaissSelection:
    started = time.perf_counter()
    ordered = bm25_order(groups, lexical_query)
    duration = int((time.perf_counter() - started) * 1000)
    return FaissSelection(
        groups=_balanced(ordered, limit),
        method="bm25_fallback",
        bm25_duration_ms=duration,
    )


def select_faiss_groups(
    groups: list[CandidateGroup],
    *,
    lexical_query: str,
    semantic_query: str,
    encoder: CandidateEmbeddingEncoder,
    limit: int,
    rank_constant: int = 60,
) -> FaissSelection:
    bm25_started = time.perf_counter()
    lexical = bm25_order(groups, lexical_query)
    bm25_duration = int((time.perf_counter() - bm25_started) * 1000)

    texts = [semantic_query, *(group_search_text(group) for group in groups)]
    metadata = encoder.metadata
    query_digest = hashlib.sha256(semantic_query.encode("utf-8")).hexdigest()
    keys = [
        ":".join(
            [metadata.model_id, metadata.revision, SEMANTIC_TEXT_VERSION, "query", query_digest]
        ),
        *(semantic_cache_key(group, encoder) for group in groups),
    ]
    embedding_started = time.perf_counter()
    batch = encoder.encode_cached(keys, texts)
    embedding_duration = int((time.perf_counter() - embedding_started) * 1000)

    faiss_started = time.perf_counter()
    index = TransientFaissFlatIndex(batch.vectors.shape[1])
    index.add(batch.vectors[1:])
    semantic_positions = index.search(batch.vectors[0], len(groups))
    semantic = [groups[position] for position, _ in semantic_positions]
    faiss_duration = int((time.perf_counter() - faiss_started) * 1000)

    ranks: dict[str, float] = defaultdict(float)
    for lane in (lexical, semantic):
        for rank, group in enumerate(lane, start=1):
            ranks[group.product_group_id] += 1 / (rank_constant + rank)
    ordered = sorted(
        groups,
        key=lambda group: (
            -ranks[group.product_group_id],
            _source_rank(group),
            group.input_order,
        ),
    )
    return FaissSelection(
        groups=_balanced(ordered, limit),
        method="faiss_rrf",
        embedding_model=metadata.model_id,
        embedding_revision=metadata.revision,
        embedding_cache_hits=batch.cache_hits,
        embedding_cache_misses=batch.cache_misses,
        embedding_duration_ms=embedding_duration,
        bm25_duration_ms=bm25_duration,
        faiss_duration_ms=faiss_duration,
    )


def selection_summary(selection: FaissSelection) -> dict[str, Any]:
    return json.loads(json.dumps(selection.__dict__, default=str, ensure_ascii=False))
