"""One-shot, auditable MySQL-to-PostgreSQL data migration for Globuy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import MetaData, Table, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.auth.service import utc_naive
from app.config import get_settings
from app.database.models import Base, OutboxEvent


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, bytes):
        return value.hex()
    return value


def _row_digest(rows: list[dict[str, Any]], primary_keys: list[str]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: tuple(str(item.get(key)) for key in primary_keys)):
        payload = json.dumps(
            {key: _json_value(value) for key, value in sorted(row.items())},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        digest.update(payload.encode())
        digest.update(b"\n")
    return digest.hexdigest()


async def _reflect_source(engine: AsyncEngine) -> MetaData:
    metadata = MetaData()
    async with engine.connect() as connection:
        await connection.run_sync(metadata.reflect)
    return metadata


def _target_values(table_name: str, row: dict[str, Any], target: Table) -> dict[str, Any]:
    values = {key: value for key, value in row.items() if key in target.c}
    if table_name == "memory_entries":
        values["keywords"] = []
        values["lifecycle_status"] = "deleted" if row.get("status") == "deleted" else "active"
        values["last_reinforced_at"] = row.get("updated_at") or row.get("created_at")
        values["reinforcement_count"] = 1
        values["archived_at"] = None
        values["purge_after"] = None
    return values


async def _copy_table(
    source: AsyncConnection,
    target: AsyncConnection,
    source_table: Table,
    target_table: Table,
    *,
    batch_size: int,
) -> dict[str, Any]:
    primary_keys = [column.name for column in source_table.primary_key.columns]
    statement = select(source_table)
    if primary_keys:
        statement = statement.order_by(*(source_table.c[name] for name in primary_keys))
    result = await source.stream(statement)
    source_rows: list[dict[str, Any]] = []
    batch: list[dict[str, Any]] = []
    async for mapping in result.mappings():
        source_row = dict(mapping)
        source_rows.append(source_row)
        batch.append(_target_values(source_table.name, source_row, target_table))
        if len(batch) >= batch_size:
            await target.execute(insert(target_table), batch)
            batch.clear()
    if batch:
        await target.execute(insert(target_table), batch)
    return {
        "source_count": len(source_rows),
        "source_digest": _row_digest(source_rows, primary_keys),
        "primary_keys": primary_keys,
        "common_columns": [name for name in source_table.c.keys() if name in target_table.c],
    }


async def _target_digest(
    connection: AsyncConnection,
    table: Table,
    columns: list[str],
    primary_keys: list[str],
) -> tuple[int, str]:
    selected = [table.c[name] for name in columns]
    statement = select(*selected)
    if primary_keys:
        statement = statement.order_by(*(table.c[name] for name in primary_keys))
    rows = [dict(item) for item in (await connection.execute(statement)).mappings()]
    return len(rows), _row_digest(rows, primary_keys)


async def migrate(
    source_url: str,
    target_url: str,
    *,
    batch_size: int = 1000,
) -> dict[str, Any]:
    if not source_url.startswith("mysql+"):
        raise ValueError("GLOBUY_MIGRATION_SOURCE_URL must be a MySQL SQLAlchemy URL")
    if not target_url.startswith("postgresql+"):
        raise ValueError("GLOBUY_DATABASE_URL must be a PostgreSQL SQLAlchemy URL")
    source_engine = create_async_engine(source_url, pool_pre_ping=True)
    target_engine = create_async_engine(target_url, pool_pre_ping=True)
    report: dict[str, Any] = {"status": "running", "tables": {}}
    try:
        source_metadata = await _reflect_source(source_engine)
        async with source_engine.connect() as source, target_engine.begin() as target:
            non_empty_tables: list[str] = []
            for target_table in Base.metadata.sorted_tables:
                count = int(
                    (await target.scalar(select(func.count()).select_from(target_table))) or 0
                )
                if count:
                    non_empty_tables.append(target_table.name)
            if non_empty_tables:
                names = ", ".join(non_empty_tables)
                raise RuntimeError(
                    f"target PostgreSQL is not empty; migration refused ({names})"
                )
            source_names = set(source_metadata.tables)
            for target_table in Base.metadata.sorted_tables:
                if target_table.name not in source_names:
                    continue
                source_table = source_metadata.tables[target_table.name]
                details = await _copy_table(
                    source,
                    target,
                    source_table,
                    target_table,
                    batch_size=batch_size,
                )
                target_count, target_digest = await _target_digest(
                    target,
                    target_table,
                    details["common_columns"],
                    details["primary_keys"],
                )
                details["target_count"] = target_count
                details["target_digest"] = target_digest
                details["verified"] = (
                    details["source_count"] == target_count
                    and details["source_digest"] == target_digest
                )
                if not details["verified"]:
                    raise RuntimeError(f"verification failed for table {target_table.name}")
                report["tables"][target_table.name] = details

            memory_table = Base.metadata.tables["memory_entries"]
            active_memories = list(
                (
                    await target.execute(
                        select(memory_table).where(
                            memory_table.c.status == "active",
                            memory_table.c.lifecycle_status == "active",
                        )
                    )
                ).mappings()
            )
            now = utc_naive()
            for memory in active_memories:
                await target.execute(
                    insert(OutboxEvent).values(
                        event_id=uuid4().hex,
                        aggregate_type="memory",
                        aggregate_id=memory["memory_id"],
                        event_type="memory.upserted",
                        aggregate_version=memory["version"],
                        payload_json={
                            "memory_id": memory["memory_id"],
                            "content": memory["content"],
                            "version": memory["version"],
                        },
                        created_at=now,
                        attempts=0,
                    )
                )
            report["memory_embedding_requests"] = len(active_memories)
        report["status"] = "verified"
        return report
    finally:
        await source_engine.dispose()
        await target_engine.dispose()


async def _main(args: argparse.Namespace) -> None:
    source_url = os.getenv("GLOBUY_MIGRATION_SOURCE_URL", "")
    settings = get_settings()
    if not source_url:
        raise RuntimeError("GLOBUY_MIGRATION_SOURCE_URL is required")
    if settings.database_url is None:
        raise RuntimeError("GLOBUY_DATABASE_URL is required")
    report = await migrate(
        source_url,
        settings.database_url.get_secret_value(),
        batch_size=args.batch_size,
    )
    destination = Path(args.manifest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "manifest": str(destination)}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--manifest", default="output/migrations/mysql-to-postgres.json")
    args = parser.parse_args()
    import asyncio

    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
