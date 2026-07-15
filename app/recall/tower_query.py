"""Query-tower embedding."""

from app.recall.tower_user import hash_embedding


class QueryTower:
    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions

    def encode(self, query: str) -> list[float]:
        return hash_embedding(query, self.dimensions)
