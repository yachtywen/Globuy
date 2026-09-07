"""Schemas and encoders shared by FAISS product search and pgvector memory."""

from app.search.encoder import (
    EmbeddingEncoder,
    EmbeddingMetadata,
    LocalOnnxEmbeddingEncoder,
)
from app.search.errors import SearchNotConfiguredError
from app.search.schemas import Candidate, ItemSearchOutput, SearchFilters

__all__ = [
    "Candidate",
    "EmbeddingEncoder",
    "EmbeddingMetadata",
    "ItemSearchOutput",
    "LocalOnnxEmbeddingEncoder",
    "SearchFilters",
    "SearchNotConfiguredError",
]
