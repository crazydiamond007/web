"""The worker starts against a database whose migration may not have landed yet.

This is not hypothetical. On Railway -- and on ECS, and on Kubernetes without an
init container -- the app and the worker deploy *concurrently*, and the migration
runs as the app's pre-deploy step. The worker reliably gets there first.

The first poll then raises 42P01 (undefined_table), which `is_retryable` correctly
refuses to retry, so the worker dies. It dies again on restart, and again, and if
the migration outlives the platform's restart budget the worker stays dead while
the API carries on returning 202 to every delivery.

Observed in production on the first Railway deploy, three crashes deep, before the
migration landed and the restart finally stuck.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from webhook_receiver.adapters.clock import SystemClock
from webhook_receiver.adapters.database import create_engine, create_session_factory
from webhook_receiver.config import Settings
from webhook_receiver.worker.main import SchemaNotReadyError, await_schema

pytestmark = pytest.mark.integration


def _settings(database_url: str, *, schema_wait_timeout_seconds: float) -> Settings:
    return Settings(
        database_url=database_url,
        admin_api_key="k",
        schema_wait_timeout_seconds=schema_wait_timeout_seconds,
        # The probe interval. Small, so the wait is exercised without slowing the suite.
        poll_interval_seconds=0.05,
        _env_file=None,
    )


def _factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(create_engine(settings))


async def test_it_returns_at_once_when_the_schema_is_there(
    database_url: str,
    migrated_schema: None,
) -> None:
    settings = _settings(database_url, schema_wait_timeout_seconds=30.0)

    # The ordinary case: the migration ran, and startup must not be delayed by the
    # machinery that exists for the case where it didn't.
    await asyncio.wait_for(
        await_schema(
            _factory(settings),
            settings=settings,
            clock=SystemClock(),
            shutdown=asyncio.Event(),
        ),
        timeout=5.0,
    )


async def test_it_gives_up_loudly_when_the_schema_never_arrives(
    database_url: str,
) -> None:
    # No `migrated_schema` fixture: this is a real, empty database. Exactly what the
    # worker met on the first Railway deploy.
    settings = _settings(database_url, schema_wait_timeout_seconds=0.5)

    with pytest.raises(SchemaNotReadyError, match="did not appear"):
        await await_schema(
            _factory(settings),
            settings=settings,
            clock=SystemClock(),
            shutdown=asyncio.Event(),
        )


async def test_it_waits_for_a_migration_that_lands_late(
    database_url: str,
    alembic_config: object,
) -> None:
    """The actual race: the worker starts, *then* the app's pre-deploy migration runs."""
    from alembic import command
    from alembic.config import Config

    assert isinstance(alembic_config, Config)
    settings = _settings(database_url, schema_wait_timeout_seconds=30.0)

    async def migrate_after_a_moment() -> None:
        await asyncio.sleep(0.3)
        await asyncio.to_thread(command.upgrade, alembic_config, "head")

    migration = asyncio.create_task(migrate_after_a_moment())
    try:
        # Starts against an empty database and must survive to see the schema appear.
        await asyncio.wait_for(
            await_schema(
                _factory(settings),
                settings=settings,
                clock=SystemClock(),
                shutdown=asyncio.Event(),
            ),
            timeout=20.0,
        )
    finally:
        await migration
        await asyncio.to_thread(command.downgrade, alembic_config, "base")
