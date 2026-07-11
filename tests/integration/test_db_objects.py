"""The views, functions, triggers, and procedure from migration 0003.

The triggers are the interesting half, and they are tested the only way that
means anything: by trying to do the forbidden thing **in raw SQL**, bypassing the
application entirely. A guard that only holds when the application is the one
asking is not a guard -- it is a convention, and the whole point of these is to be
true at 2am when a tired operator has a psql prompt open.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Connection, create_engine

pytestmark = pytest.mark.integration

APPEND_ONLY = "append-only"
TERMINAL = "terminal"
AUDIT_LOG = "audit log"


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


@pytest.fixture
def db(alembic_config: AlembicConfig, database_url: str) -> Iterator[Connection]:
    command.upgrade(alembic_config, "head")
    engine = create_engine(_sync_url(database_url))
    with engine.connect() as conn:
        _seed(conn)
        yield conn
    engine.dispose()
    command.downgrade(alembic_config, "base")


def _seed(conn: Connection) -> None:
    """One succeeded event, one account, one ledger row: balance 500."""
    conn.execute(
        sa.text(
            "INSERT INTO webhook_event (id, source, external_id, idempotency_key, event_type, "
            " entity_type, entity_id, payload, headers, signature_verified, occurred_at, "
            " status, processed_at) "
            "VALUES (1,'stripe','evt_1','evt_1','balance.credited','account','acct_1', "
            " '{}'::jsonb,'{}'::jsonb,true,now(),'succeeded',now())"
        )
    )
    conn.execute(
        sa.text("INSERT INTO account (id, external_ref, balance_minor) VALUES (1,'acct_1',500)")
    )
    conn.execute(
        sa.text(
            "INSERT INTO ledger_entry (id, account_id, event_id, amount_minor) VALUES (1,1,1,500)"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO processing_attempt (id, event_id, attempt_number, finished_at, outcome) "
            "VALUES (1,1,1,now(),'succeeded')"
        )
    )
    conn.commit()


class TestLedgerIsAppendOnly:
    """The strongest statement the schema makes.

    `uq_ledger_entry_event_id` stops an effect being applied twice. It does nothing
    about the row *afterwards* -- and an UPDATE or a DELETE would silently break
    `balance == SUM(ledger_entry.amount_minor)`, which is what NFR-1 rests on.
    """

    def test_a_ledger_row_cannot_be_amended(self, db: Connection) -> None:
        with pytest.raises(sa.exc.IntegrityError, match=APPEND_ONLY):
            db.execute(sa.text("UPDATE ledger_entry SET amount_minor = 999999 WHERE id = 1"))
        db.rollback()

    def test_a_ledger_row_cannot_be_deleted(self, db: Connection) -> None:
        with pytest.raises(sa.exc.IntegrityError, match=APPEND_ONLY):
            db.execute(sa.text("DELETE FROM ledger_entry WHERE id = 1"))
        db.rollback()

    def test_the_cascade_from_webhook_event_cannot_delete_it_either(self, db: Connection) -> None:
        # This is the one that matters. `ledger_entry.event_id` is ON DELETE
        # CASCADE, so deleting an old event would quietly take its effect with it
        # and leave every balance wrong. The trigger fires *through* the cascade.
        with pytest.raises(sa.exc.IntegrityError, match=APPEND_ONLY):
            db.execute(sa.text("DELETE FROM webhook_event WHERE id = 1"))
        db.rollback()

    def test_a_delete_is_possible_when_taken_deliberately(self, db: Connection) -> None:
        # A rule with no legitimate exception gets worked around by dropping the
        # trigger, which is worse than an escape hatch. Archival is real (NFR-12),
        # so the hatch exists -- and it is per-transaction and impossible to type by
        # accident.
        db.execute(sa.text("SET LOCAL app.allow_ledger_delete = 'on'"))
        db.execute(sa.text("DELETE FROM ledger_entry WHERE id = 1"))

        remaining = db.execute(sa.text("SELECT count(*) FROM ledger_entry")).scalar_one()
        assert remaining == 0
        db.rollback()

    def test_there_is_no_escape_hatch_for_an_update(self, db: Connection) -> None:
        # A correction is a new, compensating entry. That is what a ledger is for.
        db.execute(sa.text("SET LOCAL app.allow_ledger_delete = 'on'"))

        with pytest.raises(sa.exc.IntegrityError, match=APPEND_ONLY):
            db.execute(sa.text("UPDATE ledger_entry SET amount_minor = 1 WHERE id = 1"))
        db.rollback()


class TestAuditLogIsImmutable:
    def test_an_attempt_cannot_be_rewritten(self, db: Connection) -> None:
        with pytest.raises(sa.exc.IntegrityError, match=AUDIT_LOG):
            db.execute(sa.text("UPDATE processing_attempt SET outcome = 'succeeded' WHERE id = 1"))
        db.rollback()

    def test_an_attempt_can_still_be_purged(self, db: Connection) -> None:
        # DELETE is allowed where the ledger's is not, and the difference is
        # deliberate: an attempt is a *diagnostic* and has a retention policy; a
        # ledger row is *money* and does not.
        db.execute(sa.text("DELETE FROM processing_attempt WHERE id = 1"))

        assert db.execute(sa.text("SELECT count(*) FROM processing_attempt")).scalar_one() == 0
        db.rollback()


class TestDlqTerminalIsTerminal:
    def _dead_letter(self, db: Connection, status: str = "needs_review") -> None:
        db.execute(
            sa.text(
                "INSERT INTO dead_letter_entry (id, event_id, reason, attempts_made, status) "
                "VALUES (1, 1, 'boom', 1, CAST(:status AS dlq_status))"
            ),
            {"status": status},
        )
        db.commit()

    def test_an_open_entry_can_be_resolved(self, db: Connection) -> None:
        self._dead_letter(db)

        db.execute(
            sa.text("UPDATE dead_letter_entry SET status='resolved', resolved_at=now() WHERE id=1")
        )

        assert (
            db.execute(sa.text("SELECT status FROM dead_letter_entry WHERE id=1")).scalar_one()
            == "resolved"
        )
        db.rollback()

    def test_a_resolved_entry_cannot_reopen_even_from_psql(self, db: Connection) -> None:
        # The application already refuses this with a 409 (domain/dlq.py). The
        # trigger refuses it to everything the application is not: a script, an
        # ad-hoc UPDATE, a future service. An entry a human ruled on stays ruled on.
        self._dead_letter(db, status="resolved")

        with pytest.raises(sa.exc.IntegrityError, match=TERMINAL):
            db.execute(sa.text("UPDATE dead_letter_entry SET status='needs_review' WHERE id=1"))
        db.rollback()

    def test_a_discarded_entry_cannot_be_resurrected(self, db: Connection) -> None:
        self._dead_letter(db, status="discarded")

        with pytest.raises(sa.exc.IntegrityError, match=TERMINAL):
            db.execute(sa.text("UPDATE dead_letter_entry SET status='replaying' WHERE id=1"))
        db.rollback()

    def test_a_terminal_entry_can_still_be_annotated(self, db: Connection) -> None:
        # Only a *status change* is refused. Adding a note to a resolved entry is
        # ordinary record-keeping, not a resurrection.
        self._dead_letter(db, status="resolved")

        db.execute(sa.text("UPDATE dead_letter_entry SET resolution_note='see #4471' WHERE id=1"))

        note = db.execute(
            sa.text("SELECT resolution_note FROM dead_letter_entry WHERE id=1")
        ).scalar_one()
        assert note == "see #4471"
        db.rollback()


class TestReconciliation:
    """NFR-1, as a query rather than a claim."""

    def test_a_healthy_ledger_has_no_drift(self, db: Connection) -> None:
        row = db.execute(
            sa.text("SELECT balance_minor, ledger_sum, drift FROM v_account_reconciliation")
        ).one()

        assert (row.balance_minor, row.ledger_sum, row.drift) == (500, 500, 0)
        assert db.execute(sa.text("SELECT fn_ledger_invariant_ok()")).scalar_one() is True

    def test_drift_is_detected_when_a_balance_is_edited_by_hand(self, db: Connection) -> None:
        # The balance is a *cache*; the ledger is the truth. This is the query that
        # notices when the two have parted company -- which is what a double-applied
        # effect, or a manual "fix", actually looks like from the outside.
        db.execute(sa.text("UPDATE account SET balance_minor = 123 WHERE id = 1"))

        drift = db.execute(sa.text("SELECT drift FROM v_account_reconciliation")).scalar_one()

        assert drift == -377
        assert db.execute(sa.text("SELECT fn_ledger_invariant_ok()")).scalar_one() is False
        db.rollback()

    def test_the_ledger_balance_can_be_recomputed_from_scratch(self, db: Connection) -> None:
        proved = db.execute(sa.text("SELECT fn_account_balance('acct_1')")).scalar_one()

        assert proved == 500

    def test_an_unknown_account_has_a_zero_balance_not_an_error(self, db: Connection) -> None:
        assert db.execute(sa.text("SELECT fn_account_balance('nobody')")).scalar_one() == 0


class TestOperationalViews:
    def test_queue_health_separates_due_from_waiting_on_backoff(self, db: Connection) -> None:
        # A thousand pending events are not an incident if they are all sitting out
        # a retry delay. A thousand *due* events mean the workers cannot keep up.
        # Collapsing the two into one "pending" number hides the difference.
        db.execute(
            sa.text(
                "INSERT INTO webhook_event (id, source, external_id, idempotency_key, "
                " event_type, entity_type, entity_id, payload, headers, signature_verified, "
                " occurred_at, status, next_attempt_at) VALUES "
                "(10,'stripe','due','due','balance.credited','account','acct_1','{}'::jsonb, "
                " '{}'::jsonb,true,now(),'pending', now() - interval '1 minute'), "
                "(11,'stripe','later','later','balance.credited','account','acct_1','{}'::jsonb, "
                " '{}'::jsonb,true,now(),'pending', now() + interval '1 hour')"
            )
        )

        row = db.execute(sa.text("SELECT * FROM v_queue_health")).one()

        assert row.pending == 2
        assert row.due_now == 1
        assert row.waiting_on_backoff == 1
        assert row.succeeded == 1
        assert row.oldest_due_age.total_seconds() >= 60
        db.rollback()

    def test_queue_lag_is_the_age_of_the_oldest_due_event(self, db: Connection) -> None:
        assert db.execute(sa.text("SELECT fn_queue_lag()")).scalar_one().total_seconds() == 0

    def test_event_overview_shows_whether_an_effect_exists(self, db: Connection) -> None:
        # `succeeded` with `has_effect = false` is not a bug -- it is a superseded
        # event (FR-10), correctly handled and correctly not applied. Seeing that at
        # a glance is the difference between a five-minute triage and an afternoon.
        row = db.execute(sa.text("SELECT * FROM v_event_overview WHERE id = 1")).one()

        assert row.has_effect is True
        assert row.last_outcome == "succeeded"
        assert row.attempts_recorded == 1

    def test_no_view_exposes_a_payload_or_headers(self, db: Connection) -> None:
        # NFR-6. A view is the easiest possible way to leak a body into a support
        # ticket, so the omission has to be enforced, not remembered.
        columns = db.execute(
            sa.text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name LIKE 'v\\_%'"
            )
        ).all()

        leaked = [(t, c) for t, c in columns if c in {"payload", "headers"}]
        assert leaked == []

    def test_the_open_dlq_view_hides_settled_entries(self, db: Connection) -> None:
        db.execute(
            sa.text(
                "INSERT INTO dead_letter_entry (event_id, reason, attempts_made, status) "
                "VALUES (1,'still broken',3,'needs_review')"
            )
        )
        db.execute(
            sa.text(
                "INSERT INTO webhook_event (id, source, external_id, idempotency_key, event_type, "
                " entity_type, entity_id, payload, headers, signature_verified, occurred_at) "
                "VALUES (9,'stripe','evt_9','evt_9','x','account','acct_1','{}'::jsonb, "
                " '{}'::jsonb,true,now())"
            )
        )
        db.execute(
            sa.text(
                "INSERT INTO dead_letter_entry (event_id, reason, attempts_made, status) "
                "VALUES (9,'dealt with',1,'resolved')"
            )
        )

        rows = db.execute(sa.text("SELECT event_id FROM v_dlq_open")).scalars().all()

        assert list(rows) == [1]  # the resolved one is not an operator's problem
        db.rollback()


class TestRetention:
    """NFR-12, and the loaded gun it is pointed at."""

    def test_an_event_with_an_effect_is_never_purged(self, db: Connection) -> None:
        # `ledger_entry.event_id` is ON DELETE CASCADE, so a naive retention sweep
        # would delete the old event, take its ledger row with it, and leave every
        # balance wrong. The procedure refuses -- and if it were ever wrong about
        # that, the append-only trigger would stop it dead.
        db.execute(sa.text("CALL sp_purge_history(interval '0 seconds')"))

        assert db.execute(sa.text("SELECT count(*) FROM webhook_event")).scalar_one() == 1
        assert db.execute(sa.text("SELECT count(*) FROM ledger_entry")).scalar_one() == 1
        db.rollback()

    def test_an_effectless_event_is_purged(self, db: Connection) -> None:
        # A superseded snapshot, or a handler that was a no-op: nothing to protect.
        db.execute(
            sa.text(
                "INSERT INTO webhook_event (id, source, external_id, idempotency_key, event_type, "
                " entity_type, entity_id, payload, headers, signature_verified, occurred_at, "
                " status, processed_at) "
                # `now()` is the TRANSACTION's start time in Postgres, so a row
                # written in this transaction can never be older than the cutoff
                # this transaction computes. Age it explicitly, or the test would
                # pass for the wrong reason.
                "VALUES (2,'stripe','evt_2','evt_2','balance.snapshot','account','acct_1', "
                " '{}'::jsonb,'{}'::jsonb,true,now(),'succeeded',now() - interval '1 hour')"
            )
        )

        db.execute(sa.text("CALL sp_purge_history(interval '0 seconds')"))

        remaining = db.execute(sa.text("SELECT id FROM webhook_event ORDER BY id")).scalars().all()
        assert list(remaining) == [1]  # the one with the effect survives
        db.rollback()

    def test_the_history_of_a_dead_lettered_event_is_kept(self, db: Connection) -> None:
        # The attempts of a succeeded event are of no interest once they are old.
        # The attempts of a dead-lettered event are the only thing an operator has
        # to go on, whatever their age.
        db.execute(sa.text("UPDATE webhook_event SET status='dead_lettered' WHERE id=1"))

        db.execute(sa.text("CALL sp_purge_history(interval '0 seconds')"))

        assert db.execute(sa.text("SELECT count(*) FROM processing_attempt")).scalar_one() == 1
        db.rollback()

    def test_nothing_recent_is_purged(self, db: Connection) -> None:
        db.execute(sa.text("CALL sp_purge_history(interval '90 days')"))

        assert db.execute(sa.text("SELECT count(*) FROM processing_attempt")).scalar_one() == 1
        db.rollback()
