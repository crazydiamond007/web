"""The migration is the schema. Exercise it against a real Postgres.

SPEC §6.1 forbids `create_all`, which means a broken migration is a broken
deploy with no fallback. These tests run it forwards, check it against the ORM
metadata for drift, and run it back down again.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from sqlalchemy import Connection, create_engine

from webhook_receiver.adapters.orm import Base

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "account",
    "dead_letter_entry",
    "ledger_entry",
    "processing_attempt",
    "replay_request",
    "webhook_event",
}
EXPECTED_ENUM_TYPES = {"webhook_status", "attempt_outcome", "dlq_status", "replay_outcome"}


def _sync_url(async_url: str) -> str:
    """Alembic's `command.*` helpers here run sync; psycopg drives them."""
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


@pytest.fixture
def migrated(alembic_config: AlembicConfig, database_url: str) -> Iterator[Connection]:
    command.upgrade(alembic_config, "head")
    engine = create_engine(_sync_url(database_url))
    with engine.connect() as conn:
        yield conn
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_schema(alembic_config: AlembicConfig) -> None:
    """Each test starts from an empty database."""
    command.downgrade(alembic_config, "base")


def test_upgrade_creates_every_table(migrated: Connection) -> None:
    tables = set(sa.inspect(migrated).get_table_names())
    assert tables.issuperset(EXPECTED_TABLES)


def test_upgrade_creates_every_enum_type(migrated: Connection) -> None:
    rows = migrated.execute(sa.text("SELECT typname FROM pg_type WHERE typtype = 'e'")).scalars()
    assert set(rows).issuperset(EXPECTED_ENUM_TYPES)


def test_attempt_outcome_enum_has_superseded(migrated: Connection) -> None:
    # ADR-0006. Adding this later would need ALTER TYPE outside a transaction.
    labels = migrated.execute(
        sa.text(
            "SELECT e.enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'attempt_outcome'"
        )
    ).scalars()
    assert "superseded" in set(labels)


def test_migration_matches_orm_metadata(migrated: Connection) -> None:
    """No drift between migrations/ and adapters/orm.py.

    This is the test that stops the two definitions of the schema diverging --
    the failure mode where the ORM says there is a unique constraint and the
    database, quietly, does not have one.
    """
    context = MigrationContext.configure(
        migrated,
        opts={"compare_type": True, "compare_server_default": True},
    )
    diff = compare_metadata(context, Base.metadata)
    assert diff == [], f"ORM and migration have drifted: {diff}"


def test_dedup_constraint_rejects_a_duplicate_delivery(migrated: Connection) -> None:
    """FR-5, enforced by Postgres rather than by application code."""
    insert = sa.text(
        "INSERT INTO webhook_event "
        "(source, external_id, idempotency_key, event_type, entity_type, entity_id, "
        " payload, headers, signature_verified, occurred_at) "
        "VALUES ('stripe', 'evt_1', 'evt_1', 'balance.credited', 'account', 'acct_1', "
        " '{}'::jsonb, '{}'::jsonb, true, now())"
    )
    migrated.execute(insert)
    migrated.commit()

    with pytest.raises(sa.exc.IntegrityError, match="uq_webhook_event_source_idempotency_key"):
        migrated.execute(insert)
        migrated.commit()


def test_ledger_unique_event_id_rejects_a_double_effect(migrated: Connection) -> None:
    """FR-6. The constraint that makes reprocessing safe."""
    migrated.execute(
        sa.text(
            "INSERT INTO webhook_event "
            "(id, source, external_id, idempotency_key, event_type, entity_type, entity_id, "
            " payload, headers, signature_verified, occurred_at) "
            "VALUES (1, 'stripe', 'evt_1', 'evt_1', 'balance.credited', 'account', 'acct_1', "
            " '{}'::jsonb, '{}'::jsonb, true, now())"
        )
    )
    migrated.execute(sa.text("INSERT INTO account (id, external_ref) VALUES (1, 'acct_1')"))
    entry = sa.text(
        "INSERT INTO ledger_entry (account_id, event_id, amount_minor) VALUES (1, 1, 500)"
    )
    migrated.execute(entry)
    migrated.commit()

    with pytest.raises(sa.exc.IntegrityError, match="uq_ledger_entry_event_id"):
        migrated.execute(entry)
        migrated.commit()


def test_downgrade_removes_every_table_and_type(
    alembic_config: AlembicConfig, database_url: str
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    engine = create_engine(_sync_url(database_url))
    with engine.connect() as conn:
        remaining = set(sa.inspect(conn).get_table_names())
        assert not (EXPECTED_TABLES & remaining)

        types = set(
            conn.execute(sa.text("SELECT typname FROM pg_type WHERE typtype='e'")).scalars()
        )
        # A downgrade that leaves orphan enum types makes the next upgrade fail.
        assert not (EXPECTED_ENUM_TYPES & types)
    engine.dispose()
