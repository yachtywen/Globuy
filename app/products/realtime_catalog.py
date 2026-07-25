"""Persist verified realtime candidates before exposing wishlist actions."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache

from app.config import get_settings
from app.database.models import Offer, OfferObservation, OutboxEvent, Product, SourceSnapshot
from app.database.session import Database
from app.products.identity import offer_id, product_id, source_item_id, stable_id
from app.search.schemas import Candidate, Platform


def _utc_naive(value: str | None) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).replace(tzinfo=None)
        except ValueError:
            pass
    return datetime.now(UTC).replace(tzinfo=None)


def _decimal(value: float | None) -> Decimal | None:
    return Decimal(str(value)).quantize(Decimal("0.01")) if value is not None else None


@lru_cache(maxsize=1)
def get_realtime_catalog_database() -> Database | None:
    settings = get_settings()
    if settings.database_url is None:
        return None
    return Database(
        settings.database_url.get_secret_value(),
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        pool_recycle=settings.database_pool_recycle_seconds,
    )


async def persist_realtime_candidates(
    database: Database,
    *,
    provider: str,
    query: str,
    platform: Platform,
    candidates: list[Candidate],
) -> list[Candidate]:
    """Upsert one provider response and return candidates backed by active offers."""

    if not candidates:
        return []
    captured_at = _utc_naive(candidates[0].data_as_of)
    request_digest = hashlib.sha256(f"{platform}:{query.strip().casefold()}".encode()).hexdigest()
    request_key = f"search:{platform}:{request_digest[:32]}"
    response_key = f"{provider}:{request_key}:{captured_at.isoformat(timespec='microseconds')}"
    snapshot_id = stable_id("snapshot", response_key)
    now = datetime.now(UTC).replace(tzinfo=None)
    persisted: list[Candidate] = []

    async with database.sessions.begin() as session:
        snapshot = await session.get(SourceSnapshot, snapshot_id)
        if snapshot is None:
            session.add(
                SourceSnapshot(
                    snapshot_id=snapshot_id,
                    provider=provider,
                    platform=platform,
                    captured_at=captured_at,
                    request_key=request_key,
                    status="complete",
                    raw_payload_path=None,
                    raw_payload_sha256=None,
                    metadata_json={
                        "query_sha256": request_digest,
                        "row_count": len(candidates),
                        "source_kind": "realtime_provider",
                    },
                    created_at=now,
                )
            )
            await session.flush()

        for candidate in candidates:
            item_id = candidate.item_id
            attributes = candidate.attributes
            pid = product_id(item_id)
            oid = offer_id(item_id)
            product = await session.get(Product, pid)
            if product is None:
                session.add(
                    Product(
                        product_id=pid,
                        title=candidate.title,
                        brand=str(attributes.get("brand")) if attributes.get("brand") else None,
                        model=str(attributes.get("model")) if attributes.get("model") else None,
                        category=str(attributes.get("category"))
                        if attributes.get("category")
                        else None,
                        description_summary=None,
                        attributes_json=attributes,
                        status="active",
                        first_seen_at=captured_at,
                        last_seen_at=captured_at,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.flush()
            else:
                product.title = candidate.title
                product.attributes_json = attributes
                product.last_seen_at = max(product.last_seen_at, captured_at)
                product.updated_at = now

            price = _decimal(candidate.price)
            rating = _decimal(candidate.rating)
            offer = await session.get(Offer, oid)
            if offer is None:
                offer = Offer(
                    offer_id=oid,
                    product_id=pid,
                    platform=platform,
                    source_item_id=source_item_id(item_id, platform),
                    source_sku_id="",
                    shop_name=str(attributes.get("shop_name"))
                    if attributes.get("shop_name")
                    else None,
                    product_url=candidate.product_url,
                    image_url=candidate.image_url,
                    currency=candidate.currency,
                    current_price=price,
                    rating_value=rating,
                    rating_scale=None,
                    sales_value=candidate.sales,
                    sales_scope="provider" if candidate.sales is not None else None,
                    first_seen_at=captured_at,
                    last_seen_at=captured_at,
                    is_active=True,
                )
                session.add(offer)
                await session.flush()
            else:
                offer.shop_name = (
                    str(attributes.get("shop_name")) if attributes.get("shop_name") else None
                )
                offer.product_url = candidate.product_url
                offer.image_url = candidate.image_url
                offer.currency = candidate.currency
                offer.current_price = price
                offer.rating_value = rating
                offer.sales_value = candidate.sales
                offer.sales_scope = "provider" if candidate.sales is not None else None
                offer.last_seen_at = max(offer.last_seen_at, captured_at)
                offer.is_active = True

            observation_id = stable_id("observation", f"{snapshot_id}:{oid}")
            if await session.get(OfferObservation, observation_id) is None:
                session.add(
                    OfferObservation(
                        observation_id=observation_id,
                        offer_id=oid,
                        snapshot_id=snapshot_id,
                        observed_at=captured_at,
                        provider_record_key=item_id,
                        price=price,
                        currency=candidate.currency,
                        rating_value=rating,
                        rating_scale=None,
                        sales_value=candidate.sales,
                        sales_scope="provider" if candidate.sales is not None else None,
                        stock_status=None,
                        raw_fields_json={
                            "source_kind": "realtime_provider",
                            "image_url": candidate.image_url,
                            "product_url": candidate.product_url,
                        },
                    )
                )
            offer.last_observation_id = observation_id

            event_id = stable_id("outbox", f"product:{snapshot_id}:{oid}")
            if await session.get(OutboxEvent, event_id) is None:
                session.add(
                    OutboxEvent(
                        event_id=event_id,
                        aggregate_type="product",
                        aggregate_id=pid,
                        event_type="product.upserted",
                        aggregate_version=max(1, int(captured_at.timestamp())),
                        payload_json={"product_id": pid, "offer_id": oid, "item_id": item_id},
                        created_at=now,
                        attempts=0,
                    )
                )
            persisted.append(
                candidate.model_copy(
                    update={
                        "product_id": pid,
                        "offer_id": oid,
                        "wishlist_eligible": True,
                    }
                )
            )
    return persisted


async def persist_with_runtime_database(
    query: str, platform: Platform, candidates: list[Candidate]
) -> tuple[list[Candidate], bool]:
    database = get_realtime_catalog_database()
    if database is None:
        return [item.model_copy(update={"wishlist_eligible": False}) for item in candidates], False
    try:
        return (
            await persist_realtime_candidates(
                database,
                provider="justone",
                query=query,
                platform=platform,
                candidates=candidates,
            ),
            True,
        )
    except Exception:
        return [item.model_copy(update={"wishlist_eligible": False}) for item in candidates], False


__all__ = [
    "get_realtime_catalog_database",
    "persist_realtime_candidates",
    "persist_with_runtime_database",
]
