"""Long-term-memory embedding adapter backed by the local BGE-small ONNX artifact.

The product-candidate FAISS chain and the pgvector long-term-memory chain now share
the same frozen ``BAAI/bge-small-zh-v1.5`` encoder (512d, normalized). Memory vectors
live only in PostgreSQL/pgvector; candidate vectors live only inside a single request's
transient FAISS index. The two vector stores stay strictly separate even though the
model is unified.

Inference uses the deployment-prepared ONNX INT8 artifact under
``GLOBUY_CANDIDATE_EMBEDDING_ONNX_PATH`` on CPU. Requests and workers never download,
export or quantize a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class EmbeddingMetadata:
    model_id: str
    revision: str
    dimensions: int
    normalized: bool = True
    semantic_text_version: str = "memory-text-v2"


class EmbeddingEncoder(Protocol):
    @property
    def metadata(self) -> EmbeddingMetadata: ...

    def encode_documents(self, texts: list[str]) -> list[list[float]]: ...

    def encode_query(self, text: str) -> list[float]: ...


class LocalOnnxEmbeddingEncoder:
    """Lazy CPU inference over the local BGE-small ONNX INT8 artifact (512d)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._model = None
        self._resolved_revision: str | None = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "缺少 sentence-transformers；请安装项目检索依赖后再启用记忆向量 lane"
            ) from exc

        model_path = Path(self.settings.candidate_embedding_onnx_path)
        if not model_path.exists():
            raise RuntimeError(
                "local BGE-small ONNX INT8 artifact is missing: "
                f"{model_path}（长期记忆与商品候选共用该产物）"
            )
        model = SentenceTransformer(
            str(model_path),
            backend="onnx",
            device="cpu",
            local_files_only=True,
            model_kwargs={"file_name": self.settings.candidate_embedding_onnx_file},
        )
        model.max_seq_length = self.settings.embedding_max_length
        manifest_path = model_path / "candidate_embedding_manifest.json"
        manifest_revision = None
        if manifest_path.exists():
            try:
                manifest_revision = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                ).get("resolved_revision")
            except (OSError, ValueError, TypeError):
                manifest_revision = None
        self._resolved_revision = (
            manifest_revision or self.settings.embedding_model_revision
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
            raise ValueError(
                "bge-small ONNX 输出维度与 GLOBUY_EMBEDDING_DIMENSIONS 不一致"
            )
        return result

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts)

    def encode_query(self, text: str) -> list[float]:
        return self._encode([text])[0]


@lru_cache(maxsize=1)
def get_embedding_encoder() -> LocalOnnxEmbeddingEncoder:
    return LocalOnnxEmbeddingEncoder(get_settings())
