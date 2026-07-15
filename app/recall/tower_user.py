"""User-tower embedding from stable preferences."""

import hashlib
import math
from collections.abc import Mapping
from typing import Any


def hash_embedding(text: str, dimensions: int = 64) -> list[float]:
    """Dependency-free deterministic embedding used until a real model is wired."""

    vector = [0.0] * dimensions
    tokens = [token for token in text.lower().replace("，", " ").split() if token]
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class UserTower:
    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions

    def encode(self, profile: Mapping[str, Any]) -> list[float]:
        text = " ".join(f"{key}:{value}" for key, value in sorted(profile.items()))
        return hash_embedding(text, self.dimensions)
