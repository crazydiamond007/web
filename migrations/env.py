"""Alembic environment.

The DSN comes from ``Settings``, not from ``alembic.ini``, so the password is
never committed (SPEC §NFR-6). ``target_metadata`` points at the ORM models so
``alembic check`` can detect drift between the mapping and the migrations.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from webhook_receiver.adapters.orm import Base
from webhook_receiver.config import Settings

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the DSN, most explicit source first.

    1. ``config.attributes["url"]`` -- set programmatically, e.g. by the
       integration tests pointing at an ephemeral Testcontainers instance.
    2. ``alembic -x url=...`` -- a one-off override on the command line.
    3. ``Settings`` -- i.e. the environment. The normal path.

    Never ``alembic.ini``: the DSN carries a password (SPEC §NFR-6).
    """
    programmatic = config.attributes.get("url")
    if isinstance(programmatic, str):
        return programmatic

    overrides = context.get_x_argument(as_dictionary=True)
    if "url" in overrides:
        return overrides["url"]

    return str(Settings().database_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DBAPI connection (`alembic upgrade --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # Native Postgres enums are created explicitly in the migration, so
        # autogenerate must not try to emit CREATE TYPE a second time.
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
