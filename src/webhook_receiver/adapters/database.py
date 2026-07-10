"""Async engine and session factory.

The application never calls ``Base.metadata.create_all`` (SPEC §6.1). Schema
comes from Alembic, and only from Alembic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from webhook_receiver.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine.

    ``pool_pre_ping`` costs one round-trip per checkout and buys us survival
    across an RDS failover, which severs pooled connections silently. Worth it
    for a service whose whole promise is "we don't lose acknowledged events".
    """
    return create_async_engine(
        str(settings.database_url),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        future=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction, committed on success and rolled back on any exception.

    Rolling back on *any* exception is what makes NFR-4 hold: a worker that dies
    mid-processing leaves no half-applied effect, because the effect insert and
    the status update share this transaction.
    """
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()
