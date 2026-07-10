"""The constraints that make this service correct, pinned as tests.

These assert against `Base.metadata`, so they need no database and run in
milliseconds. They exist because every one of these constraints is load-bearing:
delete any of them and the service still starts, still serves traffic, and
silently loses the guarantee it was built to provide.

The migration is checked against this same metadata for drift in
tests/integration/test_migrations.py.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Index, Table, UniqueConstraint

from webhook_receiver.adapters.orm import Base
from webhook_receiver.domain.enums import (
    AttemptOutcome,
    DlqStatus,
    ReplayOutcome,
    WebhookStatus,
)

EXPECTED_TABLES = {
    "account",
    "dead_letter_entry",
    "ledger_entry",
    "processing_attempt",
    "replay_request",
    "webhook_event",
}


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _unique_columns(table: Table, constraint_name: str) -> tuple[str, ...]:
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint) and constraint.name == constraint_name:
            return tuple(column.name for column in constraint.columns)
    pytest.fail(f"{table.name} has no UNIQUE constraint named {constraint_name!r}")


def test_schema_has_exactly_the_spec_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


class TestIdempotencyConstraints:
    """FR-5 and FR-6. Remove either and duplicates get processed twice."""

    def test_dedup_is_source_plus_idempotency_key(self) -> None:
        # FR-5: the dedup key is (source, idempotency_key), not external_id alone.
        # Two providers may legitimately issue the same event id.
        columns = _unique_columns(
            _table("webhook_event"), "uq_webhook_event_source_idempotency_key"
        )
        assert columns == ("source", "idempotency_key")

    def test_effect_is_unique_per_event(self) -> None:
        # FR-6 / NFR-1: this single constraint is what makes reprocessing safe.
        columns = _unique_columns(_table("ledger_entry"), "uq_ledger_entry_event_id")
        assert columns == ("event_id",)

    def test_one_dlq_entry_per_event(self) -> None:
        columns = _unique_columns(_table("dead_letter_entry"), "uq_dead_letter_entry_event_id")
        assert columns == ("event_id",)

    def test_attempt_numbers_do_not_collide(self) -> None:
        columns = _unique_columns(
            _table("processing_attempt"), "uq_processing_attempt_event_id_attempt_number"
        )
        assert columns == ("event_id", "attempt_number")


class TestWorkerPollIndex:
    """FR-7. Without this index the poll degrades to a seq scan under load."""

    def test_poll_index_leads_with_status_then_due_time(self) -> None:
        indexes: dict[str, Index] = {
            ix.name: ix for ix in _table("webhook_event").indexes if ix.name
        }
        poll = indexes["ix_webhook_event_status_next_attempt_at"]

        # Column order matters: status is the equality predicate, next_attempt_at
        # the range predicate. Reversed, the index cannot serve the poll query.
        assert tuple(c.name for c in poll.columns) == ("status", "next_attempt_at")

    def test_entity_lookup_index_exists(self) -> None:
        indexes = {ix.name for ix in _table("webhook_event").indexes}
        assert "ix_webhook_event_entity_type_entity_id" in indexes


class TestOrderingAndEffects:
    def test_account_carries_a_version_for_the_optimistic_guard(self) -> None:
        # FR-10: the guard that stops a late event clobbering newer state.
        assert "version" in _table("account").columns

    def test_event_carries_both_ordering_keys(self) -> None:
        columns = _table("webhook_event").columns
        assert "occurred_at" in columns
        assert "provider_sequence" in columns
        # Nullable: not every provider supplies a sequence number.
        assert columns["provider_sequence"].nullable is True

    def test_ledger_entry_cannot_outlive_its_account(self) -> None:
        # RESTRICT, not CASCADE: deleting an account with money movements should
        # fail, not silently destroy the audit trail.
        fks = list(_table("ledger_entry").c.account_id.foreign_keys)
        assert [fk.ondelete for fk in fks] == ["RESTRICT"]

    def test_replay_audit_survives_attempt_deletion(self) -> None:
        # SET NULL: purging attempts must not erase who asked for a replay.
        fks = list(_table("replay_request").c.resulting_attempt_id.foreign_keys)
        assert [fk.ondelete for fk in fks] == ["SET NULL"]


class TestEnumValues:
    """Enum *values* are persisted, so renaming one is a breaking schema change."""

    def test_webhook_status_values(self) -> None:
        assert [s.value for s in WebhookStatus] == [
            "pending",
            "processing",
            "succeeded",
            "dead_lettered",
        ]

    def test_attempt_outcome_includes_superseded(self) -> None:
        # Extends SPEC §3; see ADR-0006. FR-10 needs a way to record "handled,
        # correctly, by declining to apply" that is neither success nor failure.
        assert "superseded" in {o.value for o in AttemptOutcome}

    def test_dlq_status_values(self) -> None:
        assert [s.value for s in DlqStatus] == [
            "needs_review",
            "replaying",
            "resolved",
            "discarded",
        ]

    def test_replay_outcome_values(self) -> None:
        assert [o.value for o in ReplayOutcome] == [
            "succeeded",
            "skipped_already_processed",
            "failed",
        ]


class TestNoRetryingState:
    def test_status_enum_has_no_retrying_member(self) -> None:
        # A retryable failure returns the event to `pending` with a future
        # `next_attempt_at`, so one index serves first attempts and retries alike.
        assert "retrying" not in {s.value for s in WebhookStatus}
