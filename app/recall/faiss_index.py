"""Persistent Faiss HNSW index for local candidate recall."""

from pathlib import Path
from typing import Any

import faiss
import numpy as np


class FaissHNSWIndex:
    """Cosine recall implemented as inner product over normalized vectors."""

    def __init__(self, dimensions: int, *, hnsw_m: int = 32) -> None:
        self.dimensions = dimensions
        base = faiss.IndexHNSWFlat(dimensions, hnsw_m, faiss.METRIC_INNER_PRODUCT)
        self.index = faiss.IndexIDMap2(base)

    @staticmethod
    def _vectors(values: Any, dimensions: int) -> np.ndarray:
        vectors = np.asarray(values, dtype="float32")
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        if vectors.ndim != 2 or vectors.shape[1] != dimensions:
            raise ValueError(f"向量维度必须为 (*, {dimensions})，实际为 {vectors.shape}")
        vectors = np.ascontiguousarray(vectors)
        faiss.normalize_L2(vectors)
        return vectors

    def add(self, item_ids: list[int], vectors: Any) -> None:
        encoded = self._vectors(vectors, self.dimensions)
        ids = np.asarray(item_ids, dtype="int64")
        if len(ids) != len(encoded):
            raise ValueError("item_ids 与 vectors 数量不一致")
        self.index.add_with_ids(encoded, ids)

    def search(self, query: Any, limit: int = 20) -> list[tuple[int, float]]:
        if limit < 1:
            raise ValueError("limit 必须大于 0")
        scores, ids = self.index.search(self._vectors(query, self.dimensions), limit)
        return [
            (int(item_id), float(score))
            for item_id, score in zip(ids[0], scores[0], strict=True)
            if item_id >= 0
        ]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(path))

    @classmethod
    def load(cls, path: Path) -> "FaissHNSWIndex":
        index = faiss.read_index(str(path))
        result = cls.__new__(cls)
        result.dimensions = index.d
        result.index = index
        return result
