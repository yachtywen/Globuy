"""Idempotently import the verified JSONL snapshot into MySQL."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.config import get_settings
from app.database.models import (
    Offer,
    OfferObservation,
    OutboxEvent,
    Product,
    SourceSnapshot,
)
from app.database.session import Database
from app.products.identity import offer_id, product_id, source_item_id, stable_id


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _rating_scale(item: dict[str, Any]) -> Decimal | None:
    attributes = item.get("attributes")
    rating_type = attributes.get("rating_type") if isinstance(attributes, dict) else None
    return Decimal("100") if rating_type == "good_ratio_percent" else None


async def import_snapshot(path: Path, database: Database) -> dict[str, Any]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    snapshot_id = stable_id("snapshot", digest)
    captured_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).replace(tzinfo=None)
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    inserted_observations = 0
    now = datetime.now(UTC).replace(tzinfo=None)
    async with database.sessions.begin() as session:
        snapshot = await session.get(SourceSnapshot, snapshot_id)
        if snapshot is None:
            snapshot = SourceSnapshot(
                snapshot_id=snapshot_id,
                provider="offline_jsonl",
                platform="multi",
                captured_at=captured_at,
                request_key=path.name,
                status="complete",
                raw_payload_path=str(path.as_posix()),
                raw_payload_sha256=digest,
                metadata_json={"row_count": len(rows), "source_kind": "offline_snapshot"},
                created_at=now,
            )
            session.add(snapshot)
            await session.flush()
        for row in rows:
            item_id = str(row["item_id"])
            platform = str(row["platform"])
            attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
            pid = product_id(item_id)
            oid = offer_id(item_id)
            product = await session.get(Product, pid)
            if product is None:
                product = Product(
                    product_id=pid,
                    title=str(row["title"]),
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
                session.add(product)
                # Product and Offer do not have an ORM relationship that gives the
                # unit of work enough information to infer their FK insert order.
                await session.flush()
            else:
                product.title = str(row["title"])
                product.attributes_json = attributes
                product.last_seen_at = max(product.last_seen_at, captured_at)
                product.updated_at = now
            current_price = _decimal(row.get("price"))
            rating = _decimal(row.get("rating"))
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
                    product_url=row.get("product_url"),
                    image_url=row.get("image_url"),
                    currency=str(row.get("currency") or "CNY"),
                    current_price=current_price,
                    rating_value=rating,
                    rating_scale=_rating_scale(row),
                    sales_value=row.get("sales"),
                    sales_scope="unknown" if row.get("sales") is not None else None,
                    first_seen_at=captured_at,
                    last_seen_at=captured_at,
                    is_active=True,
                )
                session.add(offer)
                await session.flush()
            else:
                offer.current_price = current_price
                offer.rating_value = rating
                offer.rating_scale = _rating_scale(row)
                offer.sales_value = row.get("sales")
                offer.product_url = row.get("product_url")
                offer.image_url = row.get("image_url")
                offer.last_seen_at = max(offer.last_seen_at, captured_at)
                offer.is_active = True
            observation_id = stable_id("observation", f"{snapshot_id}:{oid}")
            observation = await session.get(OfferObservation, observation_id)
            if observation is None:
                observation = OfferObservation(
                    observation_id=observation_id,
                    offer_id=oid,
                    snapshot_id=snapshot_id,
                    observed_at=captured_at,
                    provider_record_key=item_id,
                    price=current_price,
                    currency=str(row.get("currency") or "CNY"),
                    rating_value=rating,
                    rating_scale=_rating_scale(row),
                    sales_value=row.get("sales"),
                    sales_scope="unknown" if row.get("sales") is not None else None,
                    stock_status=None,
                    raw_fields_json={
                        "rating_type": attributes.get("rating_type"),
                        "source_kind": "offline_snapshot",
                    },
                )
                session.add(observation)
                inserted_observations += 1
            offer.last_observation_id = observation_id
            event_exists = await session.scalar(
                select(OutboxEvent.event_id).where(
                    OutboxEvent.aggregate_type == "product",
                    OutboxEvent.aggregate_id == pid,
                    OutboxEvent.aggregate_version == 1,
                )
            )
            if event_exists is None:
                session.add(
                    OutboxEvent(
                        event_id=uuid4().hex,
                        aggregate_type="product",
                        aggregate_id=pid,
                        event_type="product.upserted",
                        aggregate_version=1,
                        payload_json={
                            "product_id": pid,
                            "offer_id": oid,
                            "item_id": item_id,
                        },
                        created_at=now,
                        attempts=0,
                    )
                )
    return {
        "snapshot_id": snapshot_id,
        "rows": len(rows),
        "inserted_observations": inserted_observations,
        "sha256": digest,
    }


async def _main(path: Path) -> None:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("GLOBUY_DATABASE_URL is required")
    database = Database(
        settings.database_url.get_secret_value(),
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        pool_recycle=settings.database_pool_recycle_seconds,
    )
    try:
        print(json.dumps(await import_snapshot(path, database), ensure_ascii=False, indent=2))
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=get_settings().product_dataset_path)
    args = parser.parse_args()
    asyncio.run(_main(args.path))


if __name__ == "__main__":
    main()
