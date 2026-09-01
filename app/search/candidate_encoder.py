"""Request-time embeddings for transient product-group selection.

This encoder is deliberately independent from the frozen BGE-M3 encoder used by
OpenSearch, CategoryInsight and long-term memory.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.config import Settings, get_settings


class CandidateEmbeddingNotConfiguredError(RuntimeError):
    """Raised when the prepared local model artifact is unavailable."""


@dataclass(frozen=True)
class CandidateEmbeddingMetadata:
    model_id: str
    revision: str
    dimensions: int
    backend: str


@dataclass(frozen=True)
class CandidateEmbeddingBatch:
    vectors: np.ndarray
    cache_hits: int
    cache_misses: int


class CandidateEmbeddingEncoder:
    """Lazy, process-local BGE-small encoder with a bounded TTL vector cache."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model = None
        self._resolved_revision: str | None = None
        self._resolved_backend: str | None = None
        self._cache: OrderedDict[str, tuple[float, np.ndarray]] = OrderedDict()
        self._lock = threading.RLock()

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - exercised through configured fallback
            raise CandidateEmbeddingNotConfiguredError(
                "candidate embedding runtime is not installed"
            ) from exc

        requested = self.settings.candidate_embedding_backend
        cuda_available = bool(torch.cuda.is_available())
        use_cuda = requested == "cuda" or (requested == "auto" and cuda_available)
        if requested == "cuda" and not cuda_available:
            raise CandidateEmbeddingNotConfiguredError("CUDA candidate embedding is unavailable")

        try:
            if use_cuda:
                model = SentenceTransformer(
                    self.settings.candidate_embedding_model_name,
                    revision=self.settings.candidate_embedding_model_revision,
                    device="cuda",
                    model_kwargs={"torch_dtype": torch.float16},
                    local_files_only=True,
                )
                backend = "cuda_fp16"
            else:
                model_path = Path(self.settings.candidate_embedding_onnx_path)
                if not model_path.exists():
                    raise CandidateEmbeddingNotConfiguredError(
                        "prepared ONNX INT8 candidate embedding artifact is missing"
                    )
                model = SentenceTransformer(
                    str(model_path),
                    backend="onnx",
                    device="cpu",
                    local_files_only=True,
                    model_kwargs={"file_name": self.settings.candidate_embedding_onnx_file},
                )
                backend = "onnx_int8"
        except CandidateEmbeddingNotConfiguredError:
            raise
        except Exception as exc:
            raise CandidateEmbeddingNotConfiguredError(
                "candidate embedding model could not be loaded"
            ) from exc

        model.max_seq_length = self.settings.candidate_embedding_max_length
        config = getattr(getattr(model, "_first_module", lambda: None)(), "auto_model", None)
        config = getattr(config, "config", None)
        manifest_revision = None
        if not use_cuda:
            manifest_path = Path(self.settings.candidate_embedding_onnx_path) / (
                "candidate_embedding_manifest.json"
            )
            if manifest_path.exists():
                try:
                    manifest_revision = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    ).get("resolved_revision")
                except (OSError, ValueError, TypeError):
                    manifest_revision = None
        self._resolved_revision = str(
            manifest_revision
            or getattr(config, "_commit_hash", None)
            or self.settings.candidate_embedding_model_revision
        )
        self._resolved_backend = backend
        self._model = model
        return model

    @property
    def metadata(self) -> CandidateEmbeddingMetadata:
        self._load()
        return CandidateEmbeddingMetadata(
            model_id=self.settings.candidate_embedding_model_name,
            revision=self._resolved_revision or self.settings.candidate_embedding_model_revision,
            dimensions=self.settings.candidate_embedding_dimensions,
            backend=self._resolved_backend or self.settings.candidate_embedding_backend,
        )

    def _purge_expired(self, now: float) -> None:
        ttl = self.settings.candidate_embedding_cache_ttl_seconds
        while self._cache:
            key, (created_at, _) = next(iter(self._cache.items()))
            if now - created_at <= ttl:
                break
            self._cache.pop(key, None)

    def warmup(self) -> CandidateEmbeddingMetadata:
        """Load the model and execute one short inference before serving traffic."""

        self.encode_cached(["__candidate_warmup__"], ["通勤降噪耳机"])
        return self.metadata

    def clear_cache(self) -> None:
        """Clear request-text vectors without unloading the resident model."""

        with self._lock:
            self._cache.clear()

    def encode_cached(self, keys: list[str], texts: list[str]) -> CandidateEmbeddingBatch:
        if len(keys) != len(texts):
            raise ValueError("candidate embedding keys and texts must have equal length")
        if not texts:
            return CandidateEmbeddingBatch(
                vectors=np.empty(
                    (0, self.settings.candidate_embedding_dimensions), dtype="float32"
                ),
                cache_hits=0,
                cache_misses=0,
            )

        with self._lock:
            model = self._load()
            now = time.monotonic()
            self._purge_expired(now)
            output: list[np.ndarray | None] = [None] * len(texts)
            missing_positions: list[int] = []
            missing_texts: list[str] = []
            hits = 0
            for position, key in enumerate(keys):
                cached = self._cache.get(key)
                if cached is None:
                    missing_positions.append(position)
                    missing_texts.append(texts[position])
                    continue
                created_at, vector = cached
                if now - created_at > self.settings.candidate_embedding_cache_ttl_seconds:
                    self._cache.pop(key, None)
                    missing_positions.append(position)
                    missing_texts.append(texts[position])
                    continue
                self._cache.move_to_end(key)
                output[position] = vector
                hits += 1

            if missing_texts:
                encoded = model.encode(
                    missing_texts,
                    batch_size=self.settings.candidate_embedding_batch_size,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
                encoded = np.asarray(encoded, dtype="float32")
                expected = (len(missing_texts), self.settings.candidate_embedding_dimensions)
                if encoded.shape != expected or not np.isfinite(encoded).all():
                    raise ValueError(
                        f"candidate embedding output must be finite with shape {expected}"
                    )
                for position, vector in zip(missing_positions, encoded, strict=True):
                    stable = np.ascontiguousarray(vector, dtype="float32")
                    output[position] = stable
                    if self.settings.candidate_embedding_cache_size > 0:
                        self._cache[keys[position]] = (now, stable)
                        self._cache.move_to_end(keys[position])
                while len(self._cache) > self.settings.candidate_embedding_cache_size:
                    self._cache.popitem(last=False)

            vectors = np.stack([vector for vector in output if vector is not None]).astype(
                "float32", copy=False
            )
            return CandidateEmbeddingBatch(
                vectors=np.ascontiguousarray(vectors),
                cache_hits=hits,
                cache_misses=len(missing_texts),
            )


_candidate_encoder: CandidateEmbeddingEncoder | None = None
_candidate_encoder_lock = threading.Lock()


def get_candidate_embedding_encoder() -> CandidateEmbeddingEncoder:
    global _candidate_encoder
    if _candidate_encoder is None:
        with _candidate_encoder_lock:
            if _candidate_encoder is None:
                _candidate_encoder = CandidateEmbeddingEncoder(get_settings())
    return _candidate_encoder
