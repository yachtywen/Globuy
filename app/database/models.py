"""Authoritative relational schema for Globuy."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.dialects.postgresql import TIMESTAMP as PostgreSQLTimestamp
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

UTC_DATETIME = (
    DateTime(timezone=False)
    .with_variant(MySQLDateTime(fsp=6), "mysql")
    .with_variant(PostgreSQLTimestamp(timezone=False, precision=6), "postgresql")
)
KEYWORDS_TYPE = ARRAY(String(128)).with_variant(JSON(), "sqlite").with_variant(JSON(), "mysql")
MEMORY_VECTOR_TYPE = Vector(1024).with_variant(JSON(), "sqlite").with_variant(JSON(), "mysql")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    email_normalized: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    updated_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    last_login_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)

    __table_args__ = (CheckConstraint("status IN ('active','disabled')"),)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64))
    csrf_token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    expires_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    revoked_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)


class Thread(Base):
    __tablename__ = "threads"

    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    status: Mapped[str] = mapped_column(String(24), index=True)
    active_slot: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    updated_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    archive_reason: Mapped[str | None] = mapped_column(String(64))
    last_run_id: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        CheckConstraint("status IN ('active','archived')"),
        CheckConstraint(
            "(status='active' AND active_slot=1) OR (status='archived' AND active_slot IS NULL)"
        ),
        UniqueConstraint("user_id", "active_slot", name="uq_threads_one_active_user"),
        Index("ix_threads_archive_order", "user_id", "status", "archived_at", "thread_id"),
    )


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("threads.thread_id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), index=True)
    query: Mapped[str] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    started_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    finished_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('starting','running','cancelling','succeeded',"
            "'cancelled','failed','interrupted')"
        ),
        Index("ix_runs_thread_created", "thread_id", "created_at", "run_id"),
    )


class RunResult(Base):
    __tablename__ = "run_results"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    final_text: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    completed_at: Mapped[datetime] = mapped_column(UTC_DATETIME)


class Message(Base):
    __tablename__ = "messages"

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("threads.thread_id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(24))
    content: Mapped[str] = mapped_column(Text)
    is_partial: Mapped[bool] = mapped_column(Boolean, default=False)
    ordinal: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)

    __table_args__ = (
        CheckConstraint("role IN ('user','assistant')"),
        UniqueConstraint("thread_id", "ordinal", name="uq_messages_thread_ordinal"),
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    file_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("threads.thread_id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger)
    relative_path: Mapped[str] = mapped_column(String(1024))
    sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)

    __table_args__ = (CheckConstraint("size >= 0"),)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True
    )
    client_request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation: Mapped[str] = mapped_column(String(100), primary_key=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="completed")
    response_status: Mapped[int] = mapped_column(Integer, default=200)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    expires_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, index=True)


class Product(Base):
    __tablename__ = "products"

    product_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(1000))
    brand: Mapped[str | None] = mapped_column(String(255), index=True)
    model: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(255), index=True)
    category_key: Mapped[str | None] = mapped_column(String(128), index=True)
    category_path: Mapped[list[str] | None] = mapped_column(JSON)
    semantic_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    description_summary: Mapped[str | None] = mapped_column(Text)
    attributes_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    last_seen_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    updated_at: Mapped[datetime] = mapped_column(UTC_DATETIME)


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(64), index=True)
    captured_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)
    request_key: Mapped[str] = mapped_column(String(255))
    provider_query: Mapped[str | None] = mapped_column(String(255))
    scope_id: Mapped[str | None] = mapped_column(String(128), index=True)
    page_number: Mapped[int | None] = mapped_column(Integer)
    cursor_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    response_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    raw_payload_path: Mapped[str | None] = mapped_column(String(1024))
    raw_payload_sha256: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)


class Offer(Base):
    __tablename__ = "offers"

    offer_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.product_id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(64), index=True)
    source_item_id: Mapped[str] = mapped_column(String(255))
    source_sku_id: Mapped[str] = mapped_column(String(255), default="")
    shop_name: Mapped[str | None] = mapped_column(String(255))
    product_url: Mapped[str | None] = mapped_column(String(2048))
    image_url: Mapped[str | None] = mapped_column(String(2048))
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    rating_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    rating_scale: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    sales_value: Mapped[int | None] = mapped_column(BigInteger)
    sales_scope: Mapped[str | None] = mapped_column(String(32))
    first_seen_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    last_seen_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    last_observation_id: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    inactive_reason: Mapped[str | None] = mapped_column(String(100))
    projection_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    projected_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)

    __table_args__ = (
        UniqueConstraint(
            "platform", "source_item_id", "source_sku_id", name="uq_offers_source_identity"
        ),
    )


class OfferObservation(Base):
    __tablename__ = "offer_observations"

    observation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    offer_id: Mapped[str] = mapped_column(
        ForeignKey("offers.offer_id", ondelete="CASCADE"), index=True
    )
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("source_snapshots.snapshot_id", ondelete="CASCADE"), index=True
    )
    observed_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)
    provider_record_key: Mapped[str] = mapped_column(String(255))
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    rating_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    rating_scale: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    sales_value: Mapped[int | None] = mapped_column(BigInteger)
    sales_scope: Mapped[str | None] = mapped_column(String(32))
    stock_status: Mapped[str | None] = mapped_column(String(32))
    raw_fields_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (
        UniqueConstraint(
            "offer_id", "snapshot_id", "provider_record_key", name="uq_observation_source"
        ),
    )


class CatalogScope(Base):
    __tablename__ = "catalog_scopes"

    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    category_key: Mapped[str] = mapped_column(String(128), index=True)
    platform: Mapped[str] = mapped_column(String(64), index=True)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    provider: Mapped[str] = mapped_column(String(64), index=True)
    scope_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="missing", index=True)
    newest_captured_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, index=True)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    updated_at: Mapped[datetime] = mapped_column(UTC_DATETIME)

    __table_args__ = (
        UniqueConstraint(
            "category_key",
            "platform",
            "currency",
            "provider",
            "scope_version",
            name="uq_catalog_scope_identity",
        ),
    )


class CatalogScopeOffer(Base):
    __tablename__ = "catalog_scope_offers"

    scope_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_scopes.scope_id", ondelete="CASCADE"), primary_key=True
    )
    offer_id: Mapped[str] = mapped_column(
        ForeignKey("offers.offer_id", ondelete="CASCADE"), primary_key=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    expires_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)


class CatalogHydrationRun(Base):
    __tablename__ = "catalog_hydration_runs"

    hydration_run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    group_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    intent_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    thresholds_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    platform_counts_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, index=True)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    started_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    finished_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)


class ProviderRequestLedger(Base):
    __tablename__ = "provider_request_ledger"

    request_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    hydration_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_hydration_runs.hydration_run_id", ondelete="SET NULL"), index=True
    )
    scope_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_scopes.scope_id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(64), index=True)
    normalized_query: Mapped[str] = mapped_column(String(255))
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    response_sha256: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    completed_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)


class Wishlist(Base):
    __tablename__ = "wishlists"

    wishlist_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100), default="我的心愿库")
    is_default: Mapped[bool] = mapped_column(Boolean, default=True)
    default_slot: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    updated_at: Mapped[datetime] = mapped_column(UTC_DATETIME)

    __table_args__ = (
        UniqueConstraint("user_id", "default_slot", name="uq_wishlists_default_user"),
    )


class WishlistItem(Base):
    __tablename__ = "wishlist_items"

    wishlist_item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    wishlist_id: Mapped[str] = mapped_column(
        ForeignKey("wishlists.wishlist_id", ondelete="CASCADE"), index=True
    )
    offer_id: Mapped[str] = mapped_column(
        ForeignKey("offers.offer_id", ondelete="CASCADE"), index=True
    )
    added_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    added_currency: Mapped[str] = mapped_column(String(3), default="CNY")
    added_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    source_thread_id: Mapped[str | None] = mapped_column(String(128))
    source_run_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    note: Mapped[str | None] = mapped_column(String(1000))
    last_checked_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    next_check_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, index=True)
    latest_observation_id: Mapped[str | None] = mapped_column(String(128))
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(UTC_DATETIME)

    __table_args__ = (
        CheckConstraint("status IN ('active','removed','purchased')"),
        UniqueConstraint("wishlist_id", "offer_id", name="uq_wishlist_offer"),
    )


class PriceRefreshRun(Base):
    __tablename__ = "price_refresh_runs"

    refresh_run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    started_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    finished_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    claimed_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)


class PriceRefreshItem(Base):
    __tablename__ = "price_refresh_items"

    refresh_item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    refresh_run_id: Mapped[str] = mapped_column(
        ForeignKey("price_refresh_runs.refresh_run_id", ondelete="CASCADE"), index=True
    )
    wishlist_item_id: Mapped[str] = mapped_column(
        ForeignKey("wishlist_items.wishlist_item_id", ondelete="CASCADE"), index=True
    )
    offer_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(24))
    error_code: Mapped[str | None] = mapped_column(String(100))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    observation_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)


class MemoryEntry(Base):
    __tablename__ = "memory_entries"

    memory_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list[str]] = mapped_column(KEYWORDS_TYPE, default=list)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("1"))
    source: Mapped[str] = mapped_column(String(32), default="user")
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    source_thread_id: Mapped[str | None] = mapped_column(String(128))
    source_run_id: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    updated_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    deleted_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    lifecycle_status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    last_reinforced_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    reinforcement_count: Mapped[int] = mapped_column(Integer, default=1)
    archived_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    purge_after: Mapped[datetime | None] = mapped_column(UTC_DATETIME, index=True)

    __table_args__ = (
        CheckConstraint("category IN ('blacklist','preference','history')"),
        CheckConstraint("source IN ('user','agent_confirmed','import')"),
        CheckConstraint("lifecycle_status IN ('active','archived','deleted')"),
        UniqueConstraint("user_id", "key", name="uq_memory_user_key"),
    )


class MemoryEmbedding(Base):
    __tablename__ = "memory_embeddings"

    memory_id: Mapped[str] = mapped_column(
        ForeignKey("memory_entries.memory_id", ondelete="CASCADE"), primary_key=True
    )
    embedding: Mapped[list[float]] = mapped_column(MEMORY_VECTOR_TYPE)
    embedding_model: Mapped[str] = mapped_column(String(255))
    embedding_revision: Mapped[str] = mapped_column(String(128))
    dimensions: Mapped[int] = mapped_column(Integer)
    normalized: Mapped[bool] = mapped_column(Boolean, default=True)
    semantic_text_version: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    embedded_at: Mapped[datetime] = mapped_column(UTC_DATETIME)


class MemoryCandidate(Base):
    __tablename__ = "memory_candidates"

    candidate_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list[str]] = mapped_column(KEYWORDS_TYPE, default=list)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_thread_id: Mapped[str | None] = mapped_column(String(128))
    source_run_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    expires_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)

    __table_args__ = (
        CheckConstraint("category IN ('blacklist','preference','history')"),
        CheckConstraint("status IN ('pending','confirmed','rejected','expired')"),
        UniqueConstraint("user_id", "content_hash", "status", name="uq_memory_candidate_state"),
    )


class MemoryVersion(Base):
    __tablename__ = "memory_versions"

    memory_version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    memory_id: Mapped[str] = mapped_column(
        ForeignKey("memory_entries.memory_id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(String(24))
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME)

    __table_args__ = (UniqueConstraint("memory_id", "version", name="uq_memory_version"),)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), index=True)
    aggregate_id: Mapped[str] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    aggregate_version: Mapped[int] = mapped_column(Integer, default=1)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)
    published_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    available_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, index=True)
    claim_token: Mapped[str | None] = mapped_column(String(128), index=True)
