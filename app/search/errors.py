"""Search-layer exceptions shared by the FAISS-only product path."""


class SearchNotConfiguredError(RuntimeError):
    """Raised when required product-catalog resources are unavailable."""
