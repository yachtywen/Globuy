"""Item-tower embedding from product facts."""

from collections.abc import Mapping
from typing import Any

from app.recall.tower_user import hash_embedding


class ItemTower:
    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions

    def encode(self, item: Mapping[str, Any]) -> list[float]:
        fields = ("title", "category", "brand", "features", "description")
        text = " ".join(str(item.get(field, "")) for field in fields)
        return hash_embedding(text, self.dimensions)
