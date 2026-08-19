"""MySQL-backed bounded catalog coverage inspection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from sqlalchemy import select

from app.auth.service import utc_naive
from app.database.models import CatalogScope as CatalogScopeRow
from app.database.models import CatalogScopeOffer, Offer
from app.database.session import Database
from app.products.catalog.scope import CatalogScope

CoverageStatus = Literal["sufficient", "thin", "stale", "missing", "refreshing"]


@dataclass(frozen=True)
class CoverageResult:
    status: CoverageStatus
    scope_id: str
    fresh_count: int
    fresh_offer_ids: tuple[str, ...]
    newest_captured_at: object | None
    refresh_required: bool
    reason: str


class CatalogCoverageService:
    def __init__(self, database: Database, *, freshness_seconds: int, minimum: int) -> None:
        self.database = database
        self.freshness_seconds = freshness_seconds
        self.minimum = minimum

    async def inspect(self, scope: CatalogScope) -> CoverageResult:
        now = utc_naive()
        fresh_after = now - timedelta(seconds=self.freshness_seconds)
        async with self.database.sessions() as session:
            row = await session.get(CatalogScopeRow, scope.scope_id)
            if row is None:
                return CoverageResult(
                    "missing", scope.scope_id, 0, (), None, True, "scope_missing"
                )
            offer_ids = tuple(
                await session.scalars(
                    select(CatalogScopeOffer.offer_id)
                    .join(Offer, Offer.offer_id == CatalogScopeOffer.offer_id)
                    .where(
                        CatalogScopeOffer.scope_id == scope.scope_id,
                        CatalogScopeOffer.expires_at > now,
                        CatalogScopeOffer.last_seen_at >= fresh_after,
                        Offer.is_active.is_(True),
                        Offer.current_price.is_not(None),
                    )
                )
            )
            count = len(offer_ids)
            if row.lease_expires_at and row.lease_expires_at > now:
                status: CoverageStatus = "refreshing"
                reason = "active_hydration"
            elif count >= self.minimum:
                status, reason = "sufficient", "fresh_and_sufficient"
            elif count:
                status, reason = "thin", "fresh_but_thin"
            elif row.newest_captured_at:
                status, reason = "stale", "only_stale_members"
            else:
                status, reason = "missing", "no_members"
            return CoverageResult(
                status,
                scope.scope_id,
                count,
                offer_ids,
                row.newest_captured_at,
                status != "sufficient",
                reason,
            )
