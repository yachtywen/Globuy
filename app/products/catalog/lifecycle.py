"""Catalog TTL cleanup, Offer expiry, and blue/green product index rebuilding."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.auth.service import utc_naive
from app.config import Settings
from app.database.models import CatalogScope as CatalogScopeRow
from app.database.models import CatalogScopeOffer, Offer, OutboxEvent, Product
from app.database.session import Database
from app.search.service import ProductIndexManager, catalog_item


class CatalogLifecycleService:
    def __init__(
        self, database: Database, manager: ProductIndexManager, settings: Settings
    ) -> None:
        self.database, self.manager, self.settings = database, manager, settings

    async def cleanup_scope_members(self) -> dict[str, int]:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            expired = await session.execute(
                delete(CatalogScopeOffer).where(CatalogScopeOffer.expires_at <= now)
            )
            empty_ids = list(
                (
                    await session.scalars(
                        select(CatalogScopeRow.scope_id)
                        .outerjoin(
                            CatalogScopeOffer,
                            CatalogScopeOffer.scope_id == CatalogScopeRow.scope_id,
                        )
                        .where(
                            CatalogScopeOffer.scope_id.is_(None),
                            CatalogScopeRow.lease_expires_at.is_(None),
                        )
                    )
                ).all()
            )
            if empty_ids:
                await session.execute(
                    delete(CatalogScopeRow).where(CatalogScopeRow.scope_id.in_(empty_ids))
                )
        return {"expired_members": int(expired.rowcount or 0), "removed_scopes": len(empty_ids)}

    async def expire_offers(self, *, stale_seconds: int) -> int:
        cutoff, now = utc_naive() - timedelta(seconds=stale_seconds), utc_naive()
        async with self.database.sessions.begin() as session:
            offers = list(
                (
                    await session.scalars(
                        select(Offer)
                        .where(Offer.is_active.is_(True), Offer.last_seen_at < cutoff)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for offer in offers:
                offer.is_active = False
                offer.inactive_reason = "stale"
                session.add(
                    OutboxEvent(
                        event_id=uuid4().hex,
                        aggregate_type="product",
                        aggregate_id=offer.offer_id,
                        event_type="offer.deleted",
                        aggregate_version=1,
                        payload_json={"product_id": offer.product_id, "offer_id": offer.offer_id},
                        created_at=now,
                        attempts=0,
                        available_at=now,
                    )
                )
        return len(offers)

    async def stats(self) -> dict[str, Any]:
        alias = self.settings.opensearch_product_alias
        index_stats = self.manager.client.indices.stats(index=alias)
        totals = index_stats.get("_all", {}).get("total", {})
        docs = totals.get("docs", {})
        live, deleted = int(docs.get("count", 0)), int(docs.get("deleted", 0))
        deleted_ratio = deleted / max(1, live + deleted)
        async with self.database.sessions() as session:
            pending = int(
                await session.scalar(
                    select(func.count())
                    .select_from(OutboxEvent)
                    .where(
                        OutboxEvent.aggregate_type == "product", OutboxEvent.published_at.is_(None)
                    )
                )
                or 0
            )
            oldest = await session.scalar(
                select(func.min(OutboxEvent.created_at)).where(
                    OutboxEvent.aggregate_type == "product",
                    OutboxEvent.published_at.is_(None),
                )
            )
        return {
            "index": index_stats,
            "active_documents": live,
            "deleted_documents": deleted,
            "deleted_ratio": deleted_ratio,
            "rebuild_recommended": (
                deleted_ratio >= self.settings.product_index_deleted_ratio_threshold
            ),
            "product_outbox_pending": pending,
            "product_outbox_lag_seconds": (
                max(0, int((utc_naive() - oldest).total_seconds())) if oldest else 0
            ),
        }

    async def rebuild(self, physical_index: str) -> dict[str, Any]:
        """Build and validate a new physical index, then atomically switch the alias."""

        if not physical_index.startswith(self.settings.opensearch_product_index_prefix):
            raise ValueError("physical index must use the configured product prefix")
        async with self.database.sessions() as session:
            highwater = await session.scalar(
                select(OutboxEvent.created_at)
                .where(OutboxEvent.aggregate_type == "product")
                .order_by(OutboxEvent.created_at.desc(), OutboxEvent.event_id.desc())
                .limit(1)
            )
            rows = list(
                (
                    await session.execute(
                        select(Product, Offer)
                        .join(Offer, Offer.product_id == Product.product_id)
                        .where(
                            Product.status == "active",
                            Product.category_key.is_not(None),
                            Offer.is_active.is_(True),
                            Offer.current_price.is_not(None),
                        )
                    )
                ).all()
            )
        items = [catalog_item(product, offer) for product, offer in rows]
        if not items:
            raise RuntimeError(
                "没有具备 category_key 的活动报价；请先运行 audit 并修复品类，未识别记录不会被猜测"
            )
        build_settings = self.settings.model_copy(
            update={
                "opensearch_product_index": physical_index,
                "opensearch_product_alias": physical_index,
            }
        )
        build_manager = ProductIndexManager(
            self.manager.client, self.manager.encoder, build_settings
        )
        build_manager.ensure_pipeline()
        build_manager.ensure_index()
        index_result = build_manager.index_items_reusing(
            items, self.settings.opensearch_product_alias
        )
        changed_ids: set[str] = set()
        if highwater is not None:
            async with self.database.sessions() as session:
                changed_ids = set(
                    await session.scalars(
                        select(OutboxEvent.aggregate_id).where(
                            OutboxEvent.aggregate_type == "product",
                            OutboxEvent.created_at >= highwater,
                        )
                    )
                )
        if changed_ids:
            async with self.database.sessions() as session:
                changed_rows = list(
                    (
                        await session.execute(
                            select(Product, Offer)
                            .join(Offer, Offer.product_id == Product.product_id)
                            .where(
                                Offer.offer_id.in_(changed_ids),
                                Product.status == "active",
                                Offer.is_active.is_(True),
                                Offer.current_price.is_not(None),
                            )
                        )
                    ).all()
                )
            active_ids = {offer.offer_id for _, offer in changed_rows}
            build_manager.project_items(
                [catalog_item(product, offer) for product, offer in changed_rows],
                sorted(changed_ids - active_ids),
            )
        counts: dict[str, int] = {}
        for item in items:
            counts[item["platform"]] = counts.get(item["platform"], 0) + 1
        publish_settings = build_settings.model_copy(
            update={"opensearch_product_alias": self.settings.opensearch_product_alias}
        )
        validation = ProductIndexManager(
            self.manager.client, self.manager.encoder, publish_settings
        ).verify_and_publish(counts)
        result = {
            **index_result,
            "platform_counts": counts,
            "replayed_offers": len(changed_ids),
            **validation,
        }
        result["physical_index"] = physical_index
        result["note"] = "alias switched atomically after count/platform validation"
        return result

    async def migration_audit(self) -> dict[str, Any]:
        async with self.database.sessions() as session:
            eligible = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Offer)
                    .join(Product, Product.product_id == Offer.product_id)
                    .where(
                        Product.status == "active",
                        Product.category_key.is_not(None),
                        Offer.is_active.is_(True),
                        Offer.current_price.is_not(None),
                    )
                )
                or 0
            )
            unresolved = list(
                await session.scalars(
                    select(Offer.offer_id)
                    .join(Product, Product.product_id == Offer.product_id)
                    .where(
                        Product.status == "active",
                        Product.category_key.is_(None),
                        Offer.is_active.is_(True),
                        Offer.current_price.is_not(None),
                    )
                    .order_by(Offer.offer_id)
                    .limit(100)
                )
            )
            unresolved_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Offer)
                    .join(Product, Product.product_id == Offer.product_id)
                    .where(
                        Product.status == "active",
                        Product.category_key.is_(None),
                        Offer.is_active.is_(True),
                        Offer.current_price.is_not(None),
                    )
                )
                or 0
            )
        return {
            "eligible_offers": eligible,
            "excluded_unknown_category": unresolved_count,
            "unresolved_offer_sample": unresolved,
            "note": "category_key 缺失的 Offer 未猜测品类，也不会进入 v2 重建。",
        }
