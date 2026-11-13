"""Async engine and session factory (SQLAlchemy 2.1 asyncio, psycopg 3).

The app connects as the app role (DATABASE_URL); migrations use the migrator connection
(`infra/alembic/env.py`). One `Database` lives in the app container.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@dataclass(frozen=True)
class Database:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    async def dispose(self) -> None:
        await self.engine.dispose()


def connect(url: str) -> Database:
    """Create the engine. No connection is opened until the first query."""
    # pre_ping: the demo database sleeps after minutes without queries (operations §6), so a
    # pooled connection may be dead when it is taken from the pool.
    engine = create_async_engine(url, pool_pre_ping=True)
    # expire_on_commit=False: objects stay readable after commit without new (implicit) IO,
    # which the asyncio extension cannot do lazily.
    return Database(engine=engine, sessions=async_sessionmaker(engine, expire_on_commit=False))
