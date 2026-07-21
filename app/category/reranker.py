"""Optional local Cross-Encoder reranking for coarse CategoryCard hits."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import httpx

from app.category.schemas import CategoryCard


class RerankerError(RuntimeError):
    pass


class RerankerNotConfigured(RerankerError):
    pass


class Reranker(Protocol):
    async def score(self, query: str, candidates: Sequence[str]) -> list[float]: ...


class HttpReranker:
    def __init__(self, endpoint: str | None, *, timeout_seconds: float = 3.0) -> None:
        self.endpoint = endpoint.strip() if endpoint else None
        self.timeout_seconds = timeout_seconds

    async def score(self, query: str, candidates: Sequence[str]) -> list[float]:
        if not self.endpoint:
            raise RerankerNotConfigured("GLOBUY_RERANKER_ENDPOINT 未配置")
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self.endpoint,
                json={"query": query, "candidates": list(candidates)},
            )
            response.raise_for_status()
        values = response.json().get("scores")
        if not isinstance(values, list) or len(values) != len(candidates):
            raise RerankerError("Reranker scores 数量与候选不一致")
        scores = [float(value) for value in values]
        if any(not math.isfinite(value) for value in scores):
            raise RerankerError("Reranker 返回非有限分数")
        return scores


async def rerank_cards(
    reranker: Reranker,
    *,
    query: str,
    cards: Sequence[CategoryCard],
    top_k: int,
) -> list[CategoryCard]:
    scores = await reranker.score(query, [card.summary for card in cards])
    paired = sorted(
        enumerate(zip(scores, cards, strict=True)),
        key=lambda item: (-item[1][0], item[0]),
    )
    return [card for _, (_, card) in paired[:top_k]]
