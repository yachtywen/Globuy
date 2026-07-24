"""Transactional services for wishlists and user-managed long-term memories."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select

from app.api.errors import ApiError
from app.auth.service import utc_naive
from app.database.models import (
    IdempotencyKey,
    MemoryEntry,
    MemorySkill,
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
        "skill_id": item.skill_id,
        "category": item.category,
        "key": item.key,
        "content": item.content,
        "confidence": _decimal(item.confidence),
        "source": item.source,
        "status": item.status,
        "source_thread_id": item.source_thread_id,
        "source_run_id": item.source_run_id,
        "version": item.version,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "deleted_at": _iso(item.deleted_at),
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
                    next_check_at=next_daily_refresh(
                        now, local_hour=self.refresh_local_hour
                    ),
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
                item.next_check_at = next_daily_refresh(
                    now, local_hour=self.refresh_local_hour
                )
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

    DEFAULT_SKILLS = (
        ("通用偏好", "适用于全部购物场景的预算、品牌、材质和排除项。", ["预算", "品牌", "性价比", "材质"]),
        ("数码设备", "耳机、手机、电脑等数码产品的偏好。", ["耳机", "手机", "电脑", "数码"]),
        ("服饰穿搭", "服装、鞋包、尺码、颜色和穿搭风格偏好。", ["衣服", "鞋", "尺码", "穿搭"]),
        ("家居生活", "家居、厨房、收纳和日常用品偏好。", ["家居", "厨房", "收纳", "生活用品"]),
        ("美妆护肤", "护肤、彩妆、香水和个人护理偏好。", ["护肤", "彩妆", "美妆", "香水"]),
        ("运动户外", "运动装备、户外用品和功能性服饰偏好。", ["运动", "跑步", "健身", "户外"]),
    )

    async def _ensure_defaults(self, user_id: str) -> None:
        async with self.database.sessions.begin() as session:
            existing = set((await session.scalars(
                select(MemorySkill.name).where(MemorySkill.user_id == user_id)
            )).all())
            now = utc_naive()
            for name, description, keywords in self.DEFAULT_SKILLS:
                if name not in existing:
                    session.add(MemorySkill(skill_id=uuid4().hex, user_id=user_id, name=name,
                        description=description, trigger_keywords=keywords, is_enabled=True,
                        status="active", created_at=now, updated_at=now, deleted_at=None))
            await session.flush()
            general_id = await session.scalar(select(MemorySkill.skill_id).where(
                MemorySkill.user_id == user_id, MemorySkill.name == "通用偏好", MemorySkill.status == "active"
            ))
            if general_id:
                for memory in list((await session.scalars(select(MemoryEntry).where(
                    MemoryEntry.user_id == user_id, MemoryEntry.skill_id.is_(None), MemoryEntry.status == "active"
                ))).all()):
                    memory.skill_id = general_id

    @staticmethod
    def _skill_snapshot(item: MemorySkill, memory_count: int = 0) -> dict[str, Any]:
        return {"skill_id": item.skill_id, "name": item.name, "description": item.description,
                "trigger_keywords": item.trigger_keywords, "is_enabled": item.is_enabled,
                "status": item.status, "memory_count": memory_count,
                "created_at": _iso(item.created_at), "updated_at": _iso(item.updated_at)}

    async def list_skills(self, user_id: str) -> list[dict[str, Any]]:
        await self._ensure_defaults(user_id)
        async with self.database.sessions() as session:
            skills = list((await session.scalars(select(MemorySkill).where(
                MemorySkill.user_id == user_id, MemorySkill.status == "active"
            ).order_by(MemorySkill.created_at))).all())
            counts = dict((await session.execute(select(MemoryEntry.skill_id, func.count(MemoryEntry.memory_id)).where(
                MemoryEntry.user_id == user_id, MemoryEntry.status == "active"
            ).group_by(MemoryEntry.skill_id))).all())
        return [self._skill_snapshot(item, int(counts.get(item.skill_id, 0))) for item in skills]

    async def create_skill(self, user_id: str, *, name: str, description: str, trigger_keywords: list[str]) -> dict[str, Any]:
        now = utc_naive()
        item = MemorySkill(skill_id=uuid4().hex, user_id=user_id, name=name.strip(), description=description.strip(),
            trigger_keywords=trigger_keywords, is_enabled=True, status="active", created_at=now, updated_at=now, deleted_at=None)
        async with self.database.sessions.begin() as session:
            session.add(item)
        return self._skill_snapshot(item)

    async def update_skill(self, user_id: str, skill_id: str, **changes: Any) -> dict[str, Any]:
        async with self.database.sessions.begin() as session:
            item = await session.scalar(select(MemorySkill).where(MemorySkill.skill_id == skill_id, MemorySkill.user_id == user_id).with_for_update())
            if item is None or item.status != "active": raise ApiError(404, "MEMORY_SKILL_NOT_FOUND", "Skill 不存在")
            for field in ("name", "description", "trigger_keywords", "is_enabled"):
                value = changes.get(field)
                if value is not None: setattr(item, field, value.strip() if isinstance(value, str) else value)
            item.updated_at = utc_naive()
            return self._skill_snapshot(item)

    async def delete_skill(self, user_id: str, skill_id: str) -> None:
        await self._ensure_defaults(user_id)
        async with self.database.sessions.begin() as session:
            item = await session.scalar(select(MemorySkill).where(MemorySkill.skill_id == skill_id, MemorySkill.user_id == user_id).with_for_update())
            if item is None or item.status != "active": raise ApiError(404, "MEMORY_SKILL_NOT_FOUND", "Skill 不存在")
            general = await session.scalar(select(MemorySkill).where(MemorySkill.user_id == user_id, MemorySkill.name == "通用偏好", MemorySkill.status == "active"))
            if general is None or general.skill_id == item.skill_id: raise ApiError(409, "MEMORY_SKILL_PROTECTED", "通用偏好不能删除")
            for memory in list((await session.scalars(select(MemoryEntry).where(MemoryEntry.user_id == user_id, MemoryEntry.skill_id == skill_id, MemoryEntry.status == "active"))).all()):
                memory.skill_id = general.skill_id
                memory.version += 1
                memory.updated_at = utc_naive()
                snapshot = _memory_snapshot(memory)
                session.add(MemoryVersion(memory_version_id=uuid4().hex, memory_id=memory.memory_id, version=memory.version, operation="update", snapshot_json=snapshot, created_at=memory.updated_at))
                session.add(self._outbox(memory, "memory.upserted", snapshot, memory.updated_at))
            item.status, item.deleted_at, item.updated_at = "deleted", utc_naive(), utc_naive()

    async def _assert_skill(self, session: Any, user_id: str, skill_id: str | None) -> str | None:
        if skill_id is None: return None
        found = await session.scalar(select(MemorySkill.skill_id).where(MemorySkill.skill_id == skill_id, MemorySkill.user_id == user_id, MemorySkill.status == "active"))
        if found is None: raise ApiError(404, "MEMORY_SKILL_NOT_FOUND", "Skill 不存在或不可用")
        return str(found)

    async def list(self, user_id: str) -> list[dict[str, Any]]:
        await self._ensure_defaults(user_id)
        async with self.database.sessions() as session:
            items = list(
                (
                    await session.scalars(
                        select(MemoryEntry)
                        .where(MemoryEntry.user_id == user_id, MemoryEntry.status == "active")
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
        skill_id: str | None = None,
        source: str = "user",
    ) -> dict[str, Any]:
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            skill_id = await self._assert_skill(session, user_id, skill_id)
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
                skill_id=skill_id,
                category=category,
                key=key,
                content=content,
                confidence=confidence,
                source=source,
                status="active",
                source_thread_id=source_thread_id,
                source_run_id=source_run_id,
                version=1,
                created_at=now,
                updated_at=now,
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
        skill_id: str | None = None,
        key: str | None = None,
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
            if skill_id is not None:
                item.skill_id = await self._assert_skill(session, user_id, skill_id)
            if key is not None:
                item.key = key
            if content is not None:
                item.content = content
            if confidence is not None:
                item.confidence = confidence
            item.version += 1
            item.updated_at = now
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
