"""Async SQLAlchemy engine and transaction factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class Database:
    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        pool_size: int = 10,
        pool_recycle: int = 1800,
    ) -> None:
        options: dict[str, object] = {
            "echo": echo,
            "pool_pre_ping": True,
            "pool_recycle": pool_recycle,
        }
        if not url.startswith("sqlite+"):
            options["pool_size"] = pool_size
        self.engine: AsyncEngine = create_async_engine(url, **options)
        self.sessions = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    async def close(self) -> None:
        await self.engine.dispose()

    async def ping(self) -> None:
        from sqlalchemy import text

        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
