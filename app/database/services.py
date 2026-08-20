"""Transactional services for wishlists and user-managed long-term memories."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.api.errors import ApiError
from app.auth.service import utc_naive
from app.database.models import (
    IdempotencyKey,
    MemoryCandidate,
    MemoryEntry,
    MemoryVersion,
    Offer,
    OfferObservation,
    OutboxEvent,
    Product,
    Run,
    Thread,
    Wishlist,
    WishlistItem,
)
from app.database.session import Database
from app.memory.keywords import extract_keywords
from app.products.schedule import next_daily_refresh


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="milliseconds") + "Z"


def _decimal(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _memory_snapshot(item: MemoryEntry) -> dict[str, Any]:
    return {
        "memory_id": item.memory_id,
        "user_id": item.user_id,
        "category": item.category,
        "key": item.key,
        "content": item.content,
        "keywords": list(item.keywords or []),
        "confidence": _decimal(item.confidence),
        "source": item.source,
        "status": item.status,
        "source_thread_id": item.source_thread_id,
        "source_run_id": item.source_run_id,
        "version": item.version,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "deleted_at": _iso(item.deleted_at),
        "lifecycle_status": item.lifecycle_status,
        "last_reinforced_at": _iso(item.last_reinforced_at),
        "reinforcement_count": item.reinforcement_count,
        "archived_at": _iso(item.archived_at),
        "purge_after": _iso(item.purge_after),
    }


class WishlistService:
    def __init__(
        self, database: Database, *, refresh_hours: int = 24, refresh_local_hour: int = 3
    ) -> None:
        self.database = database
        self.refresh_hours = refresh_hours
        self.refresh_local_hour = refresh_local_hour

    async def _default(self, session: Any, user_id: str, *, lock: bool = False) -> Wishlist:
        statement = select(Wishlist).where(
            Wishlist.user_id == user_id, Wishlist.is_default.is_(True)
        )
        if lock:
            statement = statement.with_for_update()
        wishlist = await session.scalar(statement)
        if wishlist is None:
            raise ApiError(404, "WISHLIST_NOT_FOUND", "默认心愿库不存在")
        return wishlist

    @staticmethod
    def _public(item: WishlistItem, offer: Offer, product: Product) -> dict[str, Any]:
        current = offer.current_price
        delta = None
        delta_percent = None
        if (
            current is not None
            and item.added_price is not None
            and offer.currency == item.added_currency
        ):
            delta_value = current - item.added_price
            delta = float(delta_value)
            if item.added_price != 0:
                delta_percent = float(delta_value / item.added_price * Decimal("100"))
        return {
            "wishlist_item_id": item.wishlist_item_id,
            "offer_id": offer.offer_id,
            "product_id": product.product_id,
            "item_id": f"{offer.platform}:{offer.source_item_id}",
            "platform": offer.platform,
            "title": product.title,
            "image_url": offer.image_url,
            "product_url": offer.product_url,
            "added_price": _decimal(item.added_price),
            "current_price": _decimal(current),
            "currency": offer.currency,
            "price_change": delta,
            "price_change_percent": delta_percent,
            "rating": _decimal(offer.rating_value),
            "sales": offer.sales_value,
            "status": item.status,
            "target_price": _decimal(item.target_price),
            "note": item.note,
            "added_at": _iso(item.added_at),
            "last_checked_at": _iso(item.last_checked_at),
            "next_check_at": _iso(item.next_check_at),
            "failure_count": item.failure_count,
            "last_error_code": item.last_error_code,
        }

    async def get_default(self, user_id: str) -> dict[str, Any]:
        async with self.database.sessions() as session:
            wishlist = await self._default(session, user_id)
            rows = list(
                (
                    await session.execute(
                        select(WishlistItem, Offer, Product)
                        .join(Offer, Offer.offer_id == WishlistItem.offer_id)
                        .join(Product, Product.product_id == Offer.product_id)
                        .where(
                            WishlistItem.wishlist_id == wishlist.wishlist_id,
                            WishlistItem.status == "active",
                        )
                        .order_by(WishlistItem.added_at.desc())
                    )
                ).all()
            )
        return {
            "wishlist_id": wishlist.wishlist_id,
            "name": wishlist.name,
            "items": [self._public(*row) for row in rows],
        }

    async def add(
        self,
        user_id: str,
        *,
        offer_id: str,
        source_thread_id: str | None,
        source_run_id: str | None,
        client_request_id: str,
    ) -> dict[str, Any]:
        now = utc_naive()
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "offer_id": offer_id,
                    "source_thread_id": source_thread_id,
                    "source_run_id": source_run_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self.database.sessions.begin() as session:
            wishlist = await self._default(session, user_id, lock=True)
            idem = await session.scalar(
                select(IdempotencyKey)
                .where(
                    IdempotencyKey.user_id == user_id,
                    IdempotencyKey.client_request_id == client_request_id,
                    IdempotencyKey.operation == "add_wishlist_item",
                )
                .with_for_update()
            )
            if idem:
                if idem.request_hash != request_hash:
                    raise ApiError(
                        409,
                        "IDEMPOTENCY_KEY_REUSED",
                        "该幂等键已用于不同的心愿商品请求",
                    )
                return dict(idem.response_json)
            offer = await session.scalar(
                select(Offer).where(Offer.offer_id == offer_id).with_for_update()
            )
            if offer is None or not offer.is_active:
                raise ApiError(404, "OFFER_NOT_FOUND", "指定商品报价不存在")
            product = await session.get(Product, offer.product_id)
            if product is None:
                raise ApiError(404, "OFFER_NOT_FOUND", "指定商品报价不存在")
            if source_thread_id:
                owned = await session.scalar(
                    select(Thread.thread_id).where(
                        Thread.thread_id == source_thread_id, Thread.user_id == user_id
                    )
                )
                if owned is None:
                    raise ApiError(404, "THREAD_NOT_FOUND", "推荐来源会话不存在")
            if source_run_id:
                owned_run = await session.scalar(
                    select(Run.run_id)
                    .join(Thread, Thread.thread_id == Run.thread_id)
                    .where(
                        Run.run_id == source_run_id,
                        Thread.user_id == user_id,
                        Run.thread_id == source_thread_id,
                    )
                )
                if owned_run is None:
                    raise ApiError(404, "RUN_NOT_FOUND", "推荐来源运行不存在")
            item = await session.scalar(
                select(WishlistItem)
                .where(
                    WishlistItem.wishlist_id == wishlist.wishlist_id,
                    WishlistItem.offer_id == offer_id,
                )
                .with_for_update()
            )
            if item is None:
                item = WishlistItem(
                    wishlist_item_id=uuid4().hex,
                    wishlist_id=wishlist.wishlist_id,
                    offer_id=offer_id,
                    added_price=offer.current_price,
                    added_currency=offer.currency,
                    added_at=now,
                    source_thread_id=source_thread_id,
                    source_run_id=source_run_id,
                    status="active",
                    last_checked_at=offer.last_seen_at,
                    next_check_at=next_daily_refresh(now, local_hour=self.refresh_local_hour),
                    latest_observation_id=offer.last_observation_id,
                    failure_count=0,
                    updated_at=now,
                )
                session.add(item)
            else:
                item.status = "active"
                item.added_price = offer.current_price
                item.added_currency = offer.currency
                item.added_at = now
                item.source_thread_id = source_thread_id or item.source_thread_id
                item.source_run_id = source_run_id or item.source_run_id
                item.next_check_at = next_daily_refresh(now, local_hour=self.refresh_local_hour)
                item.updated_at = now
            response = self._public(item, offer, product)
            session.add(
                IdempotencyKey(
                    user_id=user_id,
                    client_request_id=client_request_id,
                    operation="add_wishlist_item",
                    request_hash=request_hash,
                    response_json=response,
                    response_status=201,
                    created_at=now,
                )
            )
        return response

    async def update_item(
        self,
        user_id: str,
        item_id: str,
        *,
        status: str | None,
        target_price: Decimal | None,
        note: str | None,
        target_price_set: bool,
        note_set: bool,
    ) -> dict[str, Any]:
        async with self.database.sessions.begin() as session:
            row = (
                await session.execute(
                    select(WishlistItem, Offer, Product)
                    .join(Wishlist, Wishlist.wishlist_id == WishlistItem.wishlist_id)
                    .join(Offer, Offer.offer_id == WishlistItem.offer_id)
                    .join(Product, Product.product_id == Offer.product_id)
                    .where(
                        WishlistItem.wishlist_item_id == item_id,
                        Wishlist.user_id == user_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise ApiError(404, "WISHLIST_ITEM_NOT_FOUND", "心愿商品不存在")
            item, offer, product = row
            if status is not None:
                item.status = status
            if target_price_set:
                item.target_price = target_price
            if note_set:
                item.note = note
            item.updated_at = utc_naive()
            return self._public(item, offer, product)

    async def remove(self, user_id: str, item_id: str) -> None:
        await self.update_item(
            user_id,
            item_id,
            status="removed",
            target_price=None,
            note=None,
            target_price_set=False,
            note_set=False,
        )

    async def history(self, user_id: str, item_id: str) -> dict[str, Any]:
        async with self.database.sessions() as session:
            item = await session.scalar(
                select(WishlistItem)
                .join(Wishlist, Wishlist.wishlist_id == WishlistItem.wishlist_id)
                .where(
                    WishlistItem.wishlist_item_id == item_id,
                    Wishlist.user_id == user_id,
                )
            )
            if item is None:
                raise ApiError(404, "WISHLIST_ITEM_NOT_FOUND", "心愿商品不存在")
            observations = list(
                (
                    await session.scalars(
                        select(OfferObservation)
                        .where(OfferObservation.offer_id == item.offer_id)
                        .order_by(OfferObservation.observed_at)
                    )
                ).all()
            )
        return {
            "wishlist_item_id": item_id,
            "added_price": _decimal(item.added_price),
            "currency": item.added_currency,
            "items": [
                {
                    "observation_id": obs.observation_id,
                    "observed_at": _iso(obs.observed_at),
                    "price": _decimal(obs.price),
                    "currency": obs.currency,
                }
                for obs in observations
            ],
        }


class MemoryService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def list(self, user_id: str, *, lifecycle_status: str = "active") -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            items = list(
                (
                    await session.scalars(
                        select(MemoryEntry)
                        .where(
                            MemoryEntry.user_id == user_id,
                            MemoryEntry.status == "active",
                            MemoryEntry.lifecycle_status == lifecycle_status,
                        )
                        .order_by(MemoryEntry.updated_at.desc(), MemoryEntry.memory_id)
                    )
                ).all()
            )
        return [_memory_snapshot(item) for item in items]

    async def create(
        self,
        user_id: str,
        *,
        category: str,
        key: str,
        content: str,
        confidence: Decimal,
        source_thread_id: str | None,
        source_run_id: str | None,
    ) -> dict[str, Any]:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            if source_thread_id:
                owned_thread = await session.scalar(
                    select(Thread.thread_id).where(
                        Thread.thread_id == source_thread_id,
                        Thread.user_id == user_id,
                    )
                )
                if owned_thread is None:
                    raise ApiError(404, "THREAD_NOT_FOUND", "记忆来源会话不存在")
            if source_run_id:
                owned_run = await session.scalar(
                    select(Run.run_id)
                    .join(Thread, Thread.thread_id == Run.thread_id)
                    .where(
                        Run.run_id == source_run_id,
                        Run.thread_id == source_thread_id,
                        Thread.user_id == user_id,
                    )
                )
                if owned_run is None:
                    raise ApiError(404, "RUN_NOT_FOUND", "记忆来源运行不存在")
            existing = await session.scalar(
                select(MemoryEntry)
                .where(MemoryEntry.user_id == user_id, MemoryEntry.key == key)
                .with_for_update()
            )
            if existing is not None:
                raise ApiError(409, "MEMORY_KEY_EXISTS", "同名长期记忆已经存在")
            item = MemoryEntry(
                memory_id=uuid4().hex,
                user_id=user_id,
                category=category,
                key=key,
                content=content,
                keywords=extract_keywords(content),
                confidence=confidence,
                source="user",
                status="active",
                source_thread_id=source_thread_id,
                source_run_id=source_run_id,
                version=1,
                created_at=now,
                updated_at=now,
                lifecycle_status="active",
                last_reinforced_at=now,
                reinforcement_count=1,
            )
            session.add(item)
            snapshot = _memory_snapshot(item)
            session.add(
                MemoryVersion(
                    memory_version_id=uuid4().hex,
                    memory_id=item.memory_id,
                    version=1,
                    operation="create",
                    snapshot_json=snapshot,
                    created_at=now,
                )
            )
            session.add(self._outbox(item, "memory.upserted", snapshot, now))
        return snapshot

    async def update(
        self,
        user_id: str,
        memory_id: str,
        *,
        category: str | None,
        content: str | None,
        confidence: Decimal | None,
    ) -> dict[str, Any]:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            item = await session.scalar(
                select(MemoryEntry)
                .where(MemoryEntry.memory_id == memory_id, MemoryEntry.user_id == user_id)
                .with_for_update()
            )
            if item is None or item.status != "active":
                raise ApiError(404, "MEMORY_NOT_FOUND", "长期记忆不存在")
            if category is not None:
                item.category = category
            if content is not None:
                item.content = content
                item.keywords = extract_keywords(content)
            if confidence is not None:
                item.confidence = confidence
            item.version += 1
            item.updated_at = now
            item.last_reinforced_at = now
            item.reinforcement_count += 1
            item.lifecycle_status = "active"
            item.archived_at = None
            item.purge_after = None
            snapshot = _memory_snapshot(item)
            session.add(
                MemoryVersion(
                    memory_version_id=uuid4().hex,
                    memory_id=item.memory_id,
                    version=item.version,
                    operation="update",
                    snapshot_json=snapshot,
                    created_at=now,
                )
            )
            session.add(self._outbox(item, "memory.upserted", snapshot, now))
        return snapshot

    async def delete(self, user_id: str, memory_id: str) -> None:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            item = await session.scalar(
                select(MemoryEntry)
                .where(MemoryEntry.memory_id == memory_id, MemoryEntry.user_id == user_id)
                .with_for_update()
            )
            if item is None or item.status != "active":
                raise ApiError(404, "MEMORY_NOT_FOUND", "长期记忆不存在")
            item.status = "deleted"
            item.lifecycle_status = "deleted"
            item.deleted_at = now
            item.updated_at = now
            item.version += 1
            snapshot = _memory_snapshot(item)
            session.add(
                MemoryVersion(
                    memory_version_id=uuid4().hex,
                    memory_id=item.memory_id,
                    version=item.version,
                    operation="delete",
                    snapshot_json=snapshot,
                    created_at=now,
                )
            )
            session.add(self._outbox(item, "memory.deleted", snapshot, now))

    async def restore(self, user_id: str, memory_id: str) -> dict[str, Any]:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            item = await session.scalar(
                select(MemoryEntry)
                .where(MemoryEntry.memory_id == memory_id, MemoryEntry.user_id == user_id)
                .with_for_update()
            )
            if item is None or item.status != "active" or item.lifecycle_status != "archived":
                raise ApiError(404, "MEMORY_NOT_ARCHIVED", "已归档长期记忆不存在")
            item.lifecycle_status = "active"
            item.archived_at = None
            item.purge_after = None
            item.last_reinforced_at = now
            item.reinforcement_count += 1
            item.updated_at = now
            item.version += 1
            snapshot = _memory_snapshot(item)
            session.add(
                MemoryVersion(
                    memory_version_id=uuid4().hex,
                    memory_id=item.memory_id,
                    version=item.version,
                    operation="restore",
                    snapshot_json=snapshot,
                    created_at=now,
                )
            )
            session.add(self._outbox(item, "memory.upserted", snapshot, now))
        return snapshot

    async def list_candidates(self, user_id: str, *, status: str = "pending") -> list[dict]:
        async with self.database.sessions() as session:
            candidates = list(
                (
                    await session.scalars(
                        select(MemoryCandidate)
                        .where(
                            MemoryCandidate.user_id == user_id,
                            MemoryCandidate.status == status,
                        )
                        .order_by(MemoryCandidate.created_at.desc())
                    )
                ).all()
            )
        return [self._candidate_snapshot(item) for item in candidates]

    async def create_candidate(
        self,
        user_id: str,
        *,
        category: str,
        key: str,
        content: str,
        confidence: Decimal,
        source_thread_id: str | None,
        source_run_id: str | None,
        ttl_days: int = 30,
    ) -> dict[str, Any]:
        now = utc_naive()
        content_hash = hashlib.sha256(
            f"{category}\0{key}\0{' '.join(content.split())}".encode()
        ).hexdigest()
        async with self.database.sessions.begin() as session:
            existing = await session.scalar(
                select(MemoryCandidate).where(
                    MemoryCandidate.user_id == user_id,
                    MemoryCandidate.content_hash == content_hash,
                    MemoryCandidate.status == "pending",
                )
            )
            if existing is not None:
                return self._candidate_snapshot(existing)
            candidate = MemoryCandidate(
                candidate_id=uuid4().hex,
                user_id=user_id,
                category=category,
                key=key,
                content=content,
                keywords=extract_keywords(content),
                confidence=confidence,
                content_hash=content_hash,
                source_thread_id=source_thread_id,
                source_run_id=source_run_id,
                status="pending",
                created_at=now,
                expires_at=now + timedelta(days=ttl_days),
            )
            session.add(candidate)
        return self._candidate_snapshot(candidate)

    async def confirm_candidate(
        self,
        user_id: str,
        candidate_id: str,
        *,
        category: str | None = None,
        key: str | None = None,
        content: str | None = None,
    ) -> dict[str, Any]:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            candidate = await session.scalar(
                select(MemoryCandidate)
                .where(
                    MemoryCandidate.candidate_id == candidate_id,
                    MemoryCandidate.user_id == user_id,
                )
                .with_for_update()
            )
            if candidate is None:
                raise ApiError(404, "MEMORY_CANDIDATE_NOT_FOUND", "长期记忆候选不存在")
            if candidate.status == "confirmed":
                existing = await session.scalar(
                    select(MemoryEntry).where(
                        MemoryEntry.user_id == user_id, MemoryEntry.key == candidate.key
                    )
                )
                if existing is not None:
                    return _memory_snapshot(existing)
            if candidate.status != "pending" or candidate.expires_at <= now:
                raise ApiError(409, "MEMORY_CANDIDATE_NOT_PENDING", "长期记忆候选已失效")
            resolved_category = category or candidate.category
            resolved_key = key or candidate.key
            resolved_content = content or candidate.content
            item = await session.scalar(
                select(MemoryEntry)
                .where(MemoryEntry.user_id == user_id, MemoryEntry.key == resolved_key)
                .with_for_update()
            )
            if item is None:
                item = MemoryEntry(
                    memory_id=uuid4().hex,
                    user_id=user_id,
                    category=resolved_category,
                    key=resolved_key,
                    content=resolved_content,
                    keywords=extract_keywords(resolved_content),
                    confidence=candidate.confidence,
                    source="agent_confirmed",
                    status="active",
                    lifecycle_status="active",
                    source_thread_id=candidate.source_thread_id,
                    source_run_id=candidate.source_run_id,
                    version=1,
                    reinforcement_count=1,
                    last_reinforced_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(item)
                operation = "create"
            else:
                item.category = resolved_category
                item.content = resolved_content
                item.keywords = extract_keywords(resolved_content)
                item.confidence = candidate.confidence
                item.source = "agent_confirmed"
                item.status = "active"
                item.lifecycle_status = "active"
                item.archived_at = None
                item.purge_after = None
                item.source_thread_id = candidate.source_thread_id
                item.source_run_id = candidate.source_run_id
                item.version += 1
                item.reinforcement_count += 1
                item.last_reinforced_at = now
                item.updated_at = now
                operation = "update"
            snapshot = _memory_snapshot(item)
            session.add(
                MemoryVersion(
                    memory_version_id=uuid4().hex,
                    memory_id=item.memory_id,
                    version=item.version,
                    operation=operation,
                    snapshot_json=snapshot,
                    created_at=now,
                )
            )
            session.add(self._outbox(item, "memory.upserted", snapshot, now))
            candidate.status = "confirmed"
            candidate.decided_at = now
        return snapshot

    async def reject_candidate(self, user_id: str, candidate_id: str) -> None:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            candidate = await session.scalar(
                select(MemoryCandidate)
                .where(
                    MemoryCandidate.candidate_id == candidate_id,
                    MemoryCandidate.user_id == user_id,
                )
                .with_for_update()
            )
            if candidate is None or candidate.status != "pending":
                raise ApiError(404, "MEMORY_CANDIDATE_NOT_FOUND", "待确认长期记忆候选不存在")
            candidate.status = "rejected"
            candidate.decided_at = now

    @staticmethod
    def _candidate_snapshot(item: MemoryCandidate) -> dict[str, Any]:
        return {
            "candidate_id": item.candidate_id,
            "category": item.category,
            "key": item.key,
            "content": item.content,
            "keywords": list(item.keywords or []),
            "confidence": _decimal(item.confidence),
            "source_thread_id": item.source_thread_id,
            "source_run_id": item.source_run_id,
            "status": item.status,
            "created_at": _iso(item.created_at),
            "expires_at": _iso(item.expires_at),
            "decided_at": _iso(item.decided_at),
        }

    @staticmethod
    def _outbox(
        item: MemoryEntry, event_type: str, payload: dict[str, Any], now: Any
    ) -> OutboxEvent:
        return OutboxEvent(
            event_id=uuid4().hex,
            aggregate_type="memory",
            aggregate_id=item.memory_id,
            event_type=event_type,
            aggregate_version=item.version,
            payload_json=payload,
            created_at=now,
            attempts=0,
        )
