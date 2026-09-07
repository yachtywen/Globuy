"""Request-local FAISS product-candidate retrieval."""

from app.recall.transient_hybrid import TransientFaissFlatIndex, select_faiss_groups

__all__ = ["TransientFaissFlatIndex", "select_faiss_groups"]
