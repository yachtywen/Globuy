from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.auth.service import utc_naive
from app.database.models import (
    Base,
    Offer,
    OfferObservation,
    PriceRefreshItem,
    Product,
    User,
    Wishlist,
    WishlistItem,
)
from app.database.session import Database
from app.products.detail_provider import ProductDetail, ProviderRegistry
from app.products.price_worker import PriceRefreshWorker


class _DetailProvider:
    def __init__(self, detail: ProductDetail | Exception) -> None:
        self.detail = detail
        self.calls = 0

    async def get_detail(self, platform: str, source_item_id: str) -> ProductDetail:
        assert platform == "jingdong"
        assert source_item_id == "sku-1"
        self.calls += 1
        if isinstance(self.detail, Exception):
            raise self.detail
        return self.detail


async def _database_with_wishlist(tmp_path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/prices.sqlite3")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = utc_naive() - timedelta(days=2)
    async with database.sessions.begin() as session:
        session.add(
            User(
                user_id="user-1",
                email_normalized="prices@example.com",
                password_hash="not-used",
                display_name="Price Test",
                status="active",
                version=1,
                created_at=now,
                updated_at=now,
                last_login_at=None,
            )
        )
        session.add(
            Product(
                product_id="product-1",
                title="Test Headphones",
                brand=None,
                model=None,
                category="headphones",
                description_summary=None,
                attributes_json={},
                status="active",
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            Offer(
                offer_id="offer-1",
                product_id="product-1",
                platform="jingdong",
                source_item_id="sku-1",
                source_sku_id="",
                shop_name=None,
                product_url=None,
                image_url=None,
                currency="CNY",
                current_price=Decimal("299.00"),
                rating_value=None,
                rating_scale=None,
                sales_value=None,
                sales_scope=None,
                first_seen_at=now,
                last_seen_at=now,
                last_observation_id=None,
                is_active=True,
            )
        )
        session.add(
            Wishlist(
                wishlist_id="wishlist-1",
                user_id="user-1",
                name="Default",
                is_default=True,
                default_slot=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            WishlistItem(
                wishlist_item_id="wishlist-item-1",
                wishlist_id="wishlist-1",
                offer_id="offer-1",
                added_price=Decimal("299.00"),
                added_currency="CNY",
                added_at=now,
                source_thread_id=None,
                source_run_id=None,
                status="active",
                target_price=None,
                note=None,
                last_checked_at=None,
                next_check_at=None,
                latest_observation_id=None,
                failure_count=0,
                last_error_code=None,
                updated_at=now,
            )
        )
    return database


@pytest.mark.asyncio
async def test_successful_refresh_updates_offer_and_appends_observation(tmp_path) -> None:
    database = await _database_with_wishlist(tmp_path)
    provider = _DetailProvider(ProductDetail(price=Decimal("249.50"), currency="CNY"))
    worker = PriceRefreshWorker(database, ProviderRegistry({"jingdong": provider}))
    try:
        result = await worker.refresh_item("wishlist-item-1")
        assert result["succeeded"] == 1
        assert provider.calls == 1
        async with database.sessions() as session:
            offer = await session.get(Offer, "offer-1")
            item = await session.get(WishlistItem, "wishlist-item-1")
            assert offer is not None and offer.current_price == Decimal("249.50")
            assert item is not None and item.failure_count == 0
            assert item.latest_observation_id == offer.last_observation_id
            assert await session.scalar(select(func.count()).select_from(OfferObservation)) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_provider_failure_preserves_price_and_schedules_retry(tmp_path) -> None:
    database = await _database_with_wishlist(tmp_path)
    provider = _DetailProvider(RuntimeError("upstream unavailable"))
    worker = PriceRefreshWorker(database, ProviderRegistry({"jingdong": provider}))
    try:
        result = await worker.refresh_item("wishlist-item-1")
        assert result["failed"] == 1
        async with database.sessions() as session:
            offer = await session.get(Offer, "offer-1")
            item = await session.get(WishlistItem, "wishlist-item-1")
            refresh_item = await session.scalar(select(PriceRefreshItem))
            assert offer is not None and offer.current_price == Decimal("299.00")
            assert item is not None and item.failure_count == 1
            assert item.last_error_code == "provider_error"
            assert item.next_check_at is not None and item.next_check_at > utc_naive()
            assert refresh_item is not None and refresh_item.status == "failed"
            assert await session.scalar(select(func.count()).select_from(OfferObservation)) == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_manual_and_daily_refresh_reuse_same_day_observation(tmp_path) -> None:
    database = await _database_with_wishlist(tmp_path)
    provider = _DetailProvider(ProductDetail(price=Decimal("259.00"), currency="CNY"))
    worker = PriceRefreshWorker(database, ProviderRegistry({"jingdong": provider}))
    try:
        manual = await worker.refresh_item("wishlist-item-1")
        async with database.sessions.begin() as session:
            item = await session.get(WishlistItem, "wishlist-item-1", with_for_update=True)
            assert item is not None
            item.next_check_at = None
        daily = await worker.run_once()
        repeated_manual = await worker.refresh_item("wishlist-item-1")

        assert manual["succeeded"] == daily["succeeded"] == repeated_manual["succeeded"] == 1
        assert provider.calls == 1
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(OfferObservation)) == 1
            assert await session.scalar(select(func.count()).select_from(PriceRefreshItem)) == 3
    finally:
        await database.close()
