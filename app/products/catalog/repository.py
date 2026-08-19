"""Transactional persistence and provider request ledger for catalog hydration."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.auth.service import utc_naive
from app.database.models import (
    CatalogHydrationRun,
    CatalogScopeOffer,
    Offer,
    OfferObservation,
    OutboxEvent,
    Product,
    ProviderRequestLedger,
    SourceSnapshot,
)
from app.database.models import (
    CatalogScope as CatalogScopeRow,
)
from app.database.session import Database
from app.products.catalog.scope import CatalogScope, ProviderRequestFingerprint
from app.products.identity import offer_id, product_id, stable_id


def _unique_page_items(items: list[dict]) -> list[dict]:
    unique: list[dict] = []
    seen_item_ids: set[str] = set()
    for item in items:
        item_key = str(item["item_id"])
        if item_key not in seen_item_ids:
            seen_item_ids.add(item_key)
            unique.append(item)
    return unique


class CatalogRepository:
    def __init__(self, database: Database, *, scope_ttl_seconds: int) -> None:
        self.database = database
        self.scope_ttl_seconds = scope_ttl_seconds

    async def ensure_scope(self, scope: CatalogScope) -> None:
        now = utc_naive()
        try:
            async with self.database.sessions.begin() as session:
                if await session.get(CatalogScopeRow, scope.scope_id) is None:
                    session.add(
                        CatalogScopeRow(
                            scope_id=scope.scope_id,
                            category_key=scope.category_key,
                            platform=scope.platform,
                            currency=scope.currency,
                            provider=scope.provider,
                            scope_version=scope.scope_version,
                            status="missing",
                            created_at=now,
                            updated_at=now,
                        )
                    )
        except IntegrityError:
            # Another process inserted the same deterministic scope first.
            pass

    async def acquire_scope_lease(self, scope_id: str, owner: str, lease_seconds: int) -> bool:
        """Claim a scope without relying on process-local locks."""

        now = utc_naive()
        async with self.database.sessions.begin() as session:
            row = await session.get(CatalogScopeRow, scope_id, with_for_update=True)
            if row is None:
                return False
            if row.lease_expires_at is not None and row.lease_expires_at > now:
                return row.lease_owner == owner
            row.lease_owner = owner
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.status = "refreshing"
            row.updated_at = now
            return True

    async def release_scope_lease(self, scope_id: str, owner: str) -> None:
        async with self.database.sessions.begin() as session:
            row = await session.get(CatalogScopeRow, scope_id, with_for_update=True)
            if row is not None and row.lease_owner == owner:
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = utc_naive()

    async def create_hydration_run(
        self,
        hydration_run_id: str,
        group_key: str,
        intent: dict,
        thresholds: dict,
        lease_seconds: int,
    ) -> None:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            session.add(
                CatalogHydrationRun(
                    hydration_run_id=hydration_run_id,
                    group_key=group_key,
                    status="running",
                    intent_json=intent,
                    thresholds_json=thresholds,
                    platform_counts_json={},
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    created_at=now,
                    started_at=now,
                )
            )

    async def finish_hydration_run(
        self,
        hydration_run_id: str,
        *,
        status: str,
        platform_counts: dict,
        stop_reason: str,
    ) -> None:
        async with self.database.sessions.begin() as session:
            row = await session.get(CatalogHydrationRun, hydration_run_id, with_for_update=True)
            if row is None:
                return
            row.status = status
            row.platform_counts_json = platform_counts
            row.stop_reason = stop_reason
            row.lease_expires_at = None
            row.finished_at = utc_naive()

    async def reserve_request(
        self, fingerprint: ProviderRequestFingerprint, hydration_run_id: str | None
    ) -> bool:
        now = utc_naive()
        try:
            async with self.database.sessions.begin() as session:
                existing = await session.get(ProviderRequestLedger, fingerprint.request_key)
                if existing is not None:
                    return False
                session.add(
                    ProviderRequestLedger(
                        request_key=fingerprint.request_key,
                        hydration_run_id=hydration_run_id,
                        scope_id=fingerprint.scope_id,
                        provider=fingerprint.provider,
                        platform=fingerprint.platform,
                        normalized_query=" ".join(fingerprint.normalized_query.split()),
                        request_json=fingerprint.model_dump(mode="json"),
                        status="reserved",
                        attempt_count=0,
                        created_at=now,
                    )
                )
        except IntegrityError:
            return False
        return True

    async def finish_request(
        self,
        request_key: str,
        *,
        status: str,
        request_id: str | None,
        duration_ms: int,
        response_sha256: str | None = None,
        attempt_count: int = 1,
    ) -> None:
        async with self.database.sessions.begin() as session:
            row = await session.get(ProviderRequestLedger, request_key, with_for_update=True)
            if row is None:
                return
            row.status = status
            row.attempt_count += max(1, attempt_count)
            row.provider_request_id = request_id
            row.duration_ms = duration_ms
            row.response_sha256 = response_sha256
            row.completed_at = utc_naive()

    async def persist_page(
        self,
        scope: CatalogScope,
        fingerprint: ProviderRequestFingerprint,
        items: list[dict],
    ) -> dict[str, int | list[str]]:
        now = utc_naive()
        expires_at = now + timedelta(seconds=self.scope_ttl_seconds)
        # Provider result pages may repeat one product (for example once per SKU or
        # promotion). The session disables autoflush, so deduplicate before adding
        # deterministic Product/Offer identities to avoid an in-transaction PK race.
        unique_items = _unique_page_items(items)
        payload_hash = hashlib.sha256(
            json.dumps(unique_items, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        snapshot_id = stable_id("snapshot", f"{fingerprint.request_key}:{payload_hash}")
        offer_ids: list[str] = []
        async with self.database.sessions.begin() as session:
            scope_row = await session.get(CatalogScopeRow, scope.scope_id)
            if scope_row is None:
                scope_row = CatalogScopeRow(
                    scope_id=scope.scope_id,
                    category_key=scope.category_key,
                    platform=scope.platform,
                    currency=scope.currency,
                    provider=scope.provider,
                    scope_version=scope.scope_version,
                    status="refreshing",
                    created_at=now,
                    updated_at=now,
                )
                session.add(scope_row)
                await session.flush()
            session.add(
                SourceSnapshot(
                    snapshot_id=snapshot_id,
                    provider=scope.provider,
                    platform=scope.platform,
                    captured_at=now,
                    request_key=fingerprint.request_key,
                    provider_query=fingerprint.normalized_query,
                    scope_id=scope.scope_id,
                    page_number=fingerprint.cursor.page,
                    cursor_json=fingerprint.cursor.model_dump(mode="json"),
                    response_sha256=payload_hash,
                    status="complete",
                    raw_payload_path=None,
                    raw_payload_sha256=None,
                    metadata_json={"row_count": len(unique_items)},
                    created_at=now,
                )
            )
            await session.flush()
            for item in unique_items:
                item_id = str(item["item_id"])
                pid, oid = product_id(item_id), offer_id(item_id)
                product = await session.get(Product, pid)
                attributes = (
                    item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
                )
                if product is None:
                    product = Product(
                        product_id=pid,
                        title=str(item["title"]),
                        brand=attributes.get("brand"),
                        model=attributes.get("model"),
                        category=attributes.get("category"),
                        category_key=scope.category_key,
                        category_path=attributes.get("category_path"),
                        description_summary=None,
                        attributes_json=attributes,
                        status="active",
                        first_seen_at=now,
                        last_seen_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(product)
                    await session.flush()
                else:
                    product.title = str(item["title"])
                    product.attributes_json = attributes
                    product.category_key = scope.category_key
                    product.last_seen_at = now
                    product.updated_at = now
                offer = await session.get(Offer, oid)
                price = Decimal(str(item["price"]))
                if offer is None:
                    offer = Offer(
                        offer_id=oid,
                        product_id=pid,
                        platform=scope.platform,
                        source_item_id=str(item["source_item_id"]),
                        source_sku_id="",
                        shop_name=attributes.get("shop_name"),
                        product_url=item.get("product_url"),
                        image_url=item.get("image_url"),
                        currency=str(item.get("currency") or "CNY"),
                        current_price=price,
                        rating_value=item.get("rating"),
                        rating_scale=None,
                        sales_value=item.get("sales"),
                        sales_scope="unknown" if item.get("sales") is not None else None,
                        first_seen_at=now,
                        last_seen_at=now,
                        is_active=True,
                    )
                    session.add(offer)
                    await session.flush()
                else:
                    offer.current_price = price
                    offer.rating_value = item.get("rating")
                    offer.sales_value = item.get("sales")
                    offer.product_url = item.get("product_url")
                    offer.image_url = item.get("image_url")
                    offer.last_seen_at = now
                    offer.is_active = True
                    offer.inactive_reason = None
                observation_id = stable_id("observation", f"{snapshot_id}:{oid}")
                if await session.get(OfferObservation, observation_id) is None:
                    session.add(
                        OfferObservation(
                            observation_id=observation_id,
                            offer_id=oid,
                            snapshot_id=snapshot_id,
                            observed_at=now,
                            provider_record_key=str(item["source_item_id"]),
                            price=price,
                            currency=offer.currency,
                            rating_value=offer.rating_value,
                            rating_scale=offer.rating_scale,
                            sales_value=offer.sales_value,
                            sales_scope=offer.sales_scope,
                            stock_status=None,
                            raw_fields_json=None,
                        )
                    )
                offer.last_observation_id = observation_id
                member = await session.get(CatalogScopeOffer, (scope.scope_id, oid))
                if member is None:
                    session.add(
                        CatalogScopeOffer(
                            scope_id=scope.scope_id,
                            offer_id=oid,
                            last_seen_at=now,
                            expires_at=expires_at,
                        )
                    )
                else:
                    member.last_seen_at, member.expires_at = now, expires_at
                session.add(
                    OutboxEvent(
                        event_id=uuid4().hex,
                        aggregate_type="product",
                        aggregate_id=oid,
                        event_type="offer.upserted",
                        aggregate_version=1,
                        payload_json={"product_id": pid, "offer_id": oid, "item_id": item_id},
                        created_at=now,
                        attempts=0,
                        available_at=now,
                    )
                )
                offer_ids.append(oid)
            unique_offer_ids = list(dict.fromkeys(offer_ids))
            scope_row.status = "sufficient" if unique_offer_ids else "thin"
            scope_row.newest_captured_at = now
            scope_row.updated_at = now
        return {"accepted": len(unique_offer_ids), "offer_ids": unique_offer_ids}
