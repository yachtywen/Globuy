"""Frozen dense-embedding adapters used by indexing and ItemSearch."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from app.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class EmbeddingMetadata:
    model_id: str
    revision: str
    dimensions: int
    normalized: bool = True
    semantic_text_version: str = "product-title-stable-attrs-v1"


class EmbeddingEncoder(Protocol):
    @property
    def metadata(self) -> EmbeddingMetadata: ...

    def encode_documents(self, texts: list[str]) -> list[list[float]]: ...

    def encode_query(self, text: str) -> list[float]: ...


def _load_local_first(factory, model_name: str, **model_kwargs):
    """Load a cached model offline, downloading only when the cache is absent."""
    try:
        return factory(model_name, local_files_only=True, **model_kwargs)
    except OSError:
        return factory(model_name, **model_kwargs)


class BgeM3Encoder:
    """Lazy local inference for the frozen BAAI/bge-m3 model."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._model = None
        self._resolved_revision: str | None = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "缺少 sentence-transformers；请安装项目检索依赖后再构建商品索引"
            ) from exc

        requested = self.settings.embedding_device
        device = (
            "cuda"
            if requested == "auto" and torch.cuda.is_available()
            else "cpu"
            if requested == "auto"
            else requested
        )
        model_kwargs = {
            "revision": self.settings.embedding_model_revision,
            "device": device,
        }
        # A built index must remain queryable without Hugging Face network access.
        # The first index build falls back to the normal download path when the
        # requested snapshot is not present in the local cache yet.
        model = _load_local_first(
            SentenceTransformer,
            self.settings.embedding_model_name,
            **model_kwargs,
        )
        model.max_seq_length = self.settings.embedding_max_length
        if device == "cuda":
            model.half()
        config = getattr(getattr(model, "_first_module", lambda: None)(), "auto_model", None)
        config = getattr(config, "config", None)
        self._resolved_revision = str(
            getattr(config, "_commit_hash", None) or self.settings.embedding_model_revision
        )
        self._model = model
        return model

    @property
    def metadata(self) -> EmbeddingMetadata:
        self._load()
        return EmbeddingMetadata(
            model_id=self.settings.embedding_model_name,
            revision=self._resolved_revision or self.settings.embedding_model_revision,
            dimensions=self.settings.embedding_dimensions,
        )

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(
            texts,
            batch_size=self.settings.embedding_batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        result = vectors.tolist()
        if any(len(vector) != self.settings.embedding_dimensions for vector in result):
            raise ValueError("BGE-M3 输出维度与 GLOBUY_EMBEDDING_DIMENSIONS 不一致")
        return result

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts)

    def encode_query(self, text: str) -> list[float]:
        return self._encode([text])[0]


@lru_cache(maxsize=1)
def get_embedding_encoder() -> BgeM3Encoder:
    return BgeM3Encoder(get_settings())
