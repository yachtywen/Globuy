"""Deterministic global 60/100/120 catalog hydration policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.search.schemas import Platform


class StopReason(StrEnum):
    TARGET_REACHED = "target_reached"
    MINIMUM_AT_DEADLINE = "minimum_at_deadline"
    HARD_CAP_REACHED = "hard_cap_reached"
    EXHAUSTED = "exhausted"
    BUDGET_REACHED = "budget_reached"
    HARD_DEADLINE = "hard_deadline"
    CANCELLED = "cancelled"


@dataclass
class CatalogStopPolicy:
    platforms: tuple[Platform, ...]
    minimum_total: int = 60
    target_total: int = 100
    hard_cap_total: int = 120
    minimum_per_platform: int = 15
    max_success_calls: int = 12
    max_attempts: int = 20
    counts: dict[Platform, int] = field(default_factory=dict)
    pages: dict[Platform, int] = field(default_factory=dict)
    exhausted: set[Platform] = field(default_factory=set)
    attempts: int = 0
    success_calls: int = 0

    def __post_init__(self) -> None:
        self.counts = {platform: self.counts.get(platform, 0) for platform in self.platforms}
        self.pages = {platform: self.pages.get(platform, 0) for platform in self.platforms}

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def observe(self, platform: Platform, accepted: int, *, success: bool, has_more: bool) -> None:
        self.attempts += 1
        if success:
            self.success_calls += 1
            self.pages[platform] += 1
            self.counts[platform] += max(0, accepted)
        if not has_more:
            self.exhausted.add(platform)

    def reason(
        self, *, soft_deadline: bool = False, hard_deadline: bool = False
    ) -> StopReason | None:
        if self.total >= self.hard_cap_total:
            return StopReason.HARD_CAP_REACHED
        if hard_deadline:
            return StopReason.HARD_DEADLINE
        if self.attempts >= self.max_attempts or self.success_calls >= self.max_success_calls:
            return StopReason.BUDGET_REACHED
        all_started = all(
            self.pages[platform] >= 1 or platform in self.exhausted for platform in self.platforms
        )
        if self.total >= self.target_total and all_started:
            return StopReason.TARGET_REACHED
        available = [platform for platform in self.platforms if platform not in self.exhausted]
        covered = all(self.counts[platform] >= self.minimum_per_platform for platform in available)
        if soft_deadline and self.total >= self.minimum_total and covered:
            return StopReason.MINIMUM_AT_DEADLINE
        if len(self.exhausted) == len(self.platforms):
            return StopReason.EXHAUSTED
        return None
