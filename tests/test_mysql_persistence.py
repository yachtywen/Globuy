"""Database-backed authentication, ownership, wishlist and memory contracts."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from app.api.server import create_app
from app.config import Settings
from app.database.models import Base, User
from app.database.session import Database
from app.products.import_snapshot import import_snapshot
from app.products.schedule import next_daily_refresh


async def _agent(query: str, thread_id: str) -> tuple[str, dict]:
    return f"完成：{query}", {"status": "ok", "thread_id": thread_id}


def _database(tmp_path, *, enforce_foreign_keys: bool = False) -> tuple[str, Database]:
    url = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/mysql-contract.sqlite3"
    database = Database(url)

    if enforce_foreign_keys:

        @event.listens_for(database.engine.sync_engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async def create() -> None:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(create())
    return url, database


def test_registration_does_not_misreport_a_child_constraint_as_existing_email(
    tmp_path,
) -> None:
    url, database = _database(tmp_path, enforce_foreign_keys=True)

    async def reject_wishlist_insert() -> None:
        async with database.engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TRIGGER reject_registration_wishlist "
                    "BEFORE INSERT ON wishlists "
                    "BEGIN SELECT RAISE(ABORT, 'forced child constraint'); END"
                )
            )

    asyncio.run(reject_wishlist_insert())
    asyncio.run(database.close())
    settings = Settings(
        database_url=url,
        output_dir=tmp_path / "output",
        uploaded_dir=tmp_path / "uploaded",
        model_provider="mock",
        web_search_provider="none",
        redis_url=None,
    )
    app = create_app(settings=settings, agent_runner=_agent)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/register",
            headers={"Idempotency-Key": "register-child-failure"},
            json={
                "email": "new-user@example.com",
                "password": "correct-horse-battery",
                "display_name": "测试用户",
            },
        )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "REGISTRATION_FAILED"

    async def user_count() -> int:
        check = Database(url)
        async with check.engine.connect() as connection:
            count = await connection.scalar(text("SELECT COUNT(*) FROM users"))
        await check.close()
        return int(count or 0)

    assert asyncio.run(user_count()) == 0


def test_registration_flushes_user_before_foreign_key_children(tmp_path) -> None:
    url, database = _database(tmp_path, enforce_foreign_keys=True)
    asyncio.run(database.close())
    settings = Settings(
        database_url=url,
        output_dir=tmp_path / "output",
        uploaded_dir=tmp_path / "uploaded",
        model_provider="mock",
        web_search_provider="none",
        redis_url=None,
    )
    app = create_app(settings=settings, agent_runner=_agent)
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            headers={"Idempotency-Key": "register-foreign-key-order"},
            json={
                "email": "foreign-key@example.com",
                "password": "correct-horse-battery",
                "display_name": "测试用户",
            },
        )
        assert registered.status_code == 201
        logged_in = client.post(
            "/api/v1/auth/login",
            json={
                "email": "foreign-key@example.com",
                "password": "correct-horse-battery",
            },
        )
        assert logged_in.status_code == 200
        headers = {"X-CSRF-Token": logged_in.json()["csrf_token"]}
        created = client.post(
            "/api/v1/threads",
            json={"current_thread_id": None, "client_request_id": "thread-fk-order"},
            headers=headers,
        )
        assert created.status_code == 201
        task = client.post(
            "/api/v1/tasks",
            json={
                "query": "验证消息外键顺序",
                "thread_id": created.json()["thread_id"],
                "client_request_id": "run-fk-order",
            },
            headers=headers,
        )
        assert task.status_code == 202


def test_mysql_schema_uses_microsecond_datetimes() -> None:
    ddl = str(CreateTable(User.__table__).compile(dialect=mysql.dialect()))
    assert "DATETIME(6)" in ddl


def test_price_refresh_uses_next_beijing_0300() -> None:
    assert next_daily_refresh(datetime(2026, 7, 21, 18), local_hour=3) == datetime(2026, 7, 21, 19)


def test_authenticated_user_data_flow(tmp_path) -> None:
    url, database = _database(tmp_path)
    dataset = tmp_path / "products.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "item_id": "jingdong:1001",
                "platform": "jingdong",
                "title": "测试耳机",
                "price": 299,
                "currency": "CNY",
                "rating": None,
                "sales": None,
                "attributes": {},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    asyncio.run(import_snapshot(dataset, database))
    asyncio.run(database.close())


    settings = Settings(
        database_url=url,
        output_dir=tmp_path / "output",
        uploaded_dir=tmp_path / "uploaded",
        model_provider="mock",
        web_search_provider="none",
        redis_url=None,
    )
    app = create_app(settings=settings, agent_runner=_agent)
    with TestClient(app) as client:
        assert client.get("/api/v1/threads").status_code == 401
        registered = client.post(
            "/api/v1/auth/register",
            headers={"Idempotency-Key": "register-request-1"},
            json={
                "email": "USER@example.com",
                "password": "correct-horse-battery",
                "display_name": "测试用户",
            },
        )
        assert registered.status_code == 201
        retried_registration = client.post(
            "/api/v1/auth/register",
            headers={"Idempotency-Key": "register-request-1"},
            json={
                "email": "user@example.com",
                "password": "correct-horse-battery",
                "display_name": "测试用户",
            },
        )
        assert retried_registration.status_code == 201
        assert (
            retried_registration.json()["user"]["user_id"] == registered.json()["user"]["user_id"]
        )
        registered = retried_registration
        csrf = registered.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}

        created = client.post(
            "/api/v1/threads",
            json={"current_thread_id": None, "client_request_id": "thread-1"},
            headers=headers,
        )
        assert created.status_code == 201
        assert client.get("/api/v1/threads", params={"status": "active"}).status_code == 200

        from app.products.identity import offer_id

        wished = client.post(
            "/api/v1/wishlists/default/items",
            json={
                "offer_id": offer_id("jingdong:1001"),
                "source_thread_id": created.json()["thread_id"],
                "source_run_id": None,
                "client_request_id": "wish-1",
            },
            headers=headers,
        )
        assert wished.status_code == 201
        assert wished.json()["price_change"] == 0.0
        assert client.get("/api/v1/wishlists/default").json()["items"][0]["sales"] is None
        replayed = client.post(
            "/api/v1/wishlists/default/items",
            json={
                "offer_id": offer_id("jingdong:1001"),
                "source_thread_id": created.json()["thread_id"],
                "source_run_id": None,
                "client_request_id": "wish-1",
            },
            headers=headers,
        )
        assert replayed.status_code == 201
        assert replayed.json()["wishlist_item_id"] == wished.json()["wishlist_item_id"]
        conflict = client.post(
            "/api/v1/wishlists/default/items",
            json={
                "offer_id": "different-offer",
                "source_thread_id": created.json()["thread_id"],
                "source_run_id": None,
                "client_request_id": "wish-1",
            },
            headers=headers,
        )
        assert conflict.status_code == 409

        removed = client.delete(
            f"/api/v1/wishlists/default/items/{wished.json()['wishlist_item_id']}",
            headers=headers,
        )
        assert removed.status_code == 204
        assert client.get("/api/v1/wishlists/default").json()["items"] == []

        memory = client.post(
            "/api/v1/memories",
            json={
                "category": "preference",
                "key": "budget_style",
                "content": "优先考虑性价比",
            },
            headers=headers,
        )
        assert memory.status_code == 201
        assert client.get("/api/v1/memories").json()["items"][0]["version"] == 1

        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
        assert client.get("/api/v1/memories").status_code == 401


def test_snapshot_import_flushes_product_before_offer_with_foreign_keys(tmp_path) -> None:
    _url, database = _database(tmp_path, enforce_foreign_keys=True)
    dataset = tmp_path / "foreign-key-products.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "item_id": "jingdong:fk-order-1",
                "platform": "jingdong",
                "title": "Foreign key ordering product",
                "price": 199,
                "currency": "CNY",
                "rating": None,
                "sales": None,
                "attributes": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = asyncio.run(import_snapshot(dataset, database))

    async def counts() -> tuple[int, int]:
        async with database.engine.connect() as connection:
            products = await connection.scalar(text("SELECT COUNT(*) FROM products"))
            offers = await connection.scalar(text("SELECT COUNT(*) FROM offers"))
        return int(products or 0), int(offers or 0)

    assert result["rows"] == 1
    assert asyncio.run(counts()) == (1, 1)
    asyncio.run(database.close())
