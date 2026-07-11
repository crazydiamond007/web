"""operational views, functions, triggers, and a retention procedure

What belongs in the database and what does not.

The rule this migration follows: **the database gets the things it is genuinely
better at than the application, and nothing else.** It is better at enforcing that
a row can never be changed. It is better at answering "is the invariant still
true?" over a million rows. It is not better at deciding what a webhook means, and
a trigger that tried to would become a second implementation of the effect --
untyped, untested, invisible in code review, and the one with the bug.

So:

* **Triggers** enforce *immutability and terminality* -- facts that must hold no
  matter who is at the keyboard, including a tired operator with a psql prompt at
  2am. These cannot be enforced in the application, because the application is not
  in the room.
* **Views** are the operator's read model: queue health, the DLQ, and -- the one
  that matters -- the ledger reconciliation that turns NFR-1 from a claim into a
  query.
* **The procedure** is data lifecycle (NFR-12), which is inherently a bulk set
  operation and has no business round-tripping through Python.

What is deliberately NOT here is documented at the bottom of this file. It is the
more interesting half.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- Triggers: the invariants the application cannot defend -------------------

TRIGGER_FUNCTIONS = """
-- `account.updated_at` should say when the row last changed, and it should say so
-- even when the writer forgot. The application does set it; this makes it true
-- regardless, including for a manual UPDATE during an incident.
CREATE FUNCTION fn_set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;
--@@
-- The ledger is append-only. This is the strongest statement this schema makes.
--
-- `uq_ledger_entry_event_id` already stops an event's effect being applied twice.
-- It does not stop somebody UPDATEing the amount afterwards, or DELETEing the row
-- -- and either would silently break `balance == SUM(ledger_entry.amount_minor)`,
-- the invariant the entire correctness argument (NFR-1) rests on. A unique
-- constraint guards the insert; nothing guarded the row afterwards. Now something
-- does.
--
-- DELETE has an escape hatch because retention/archival is a real requirement
-- (NFR-12) and a rule with no legitimate exception gets worked around by dropping
-- the trigger, which is worse. It must be taken deliberately, per transaction:
--
--     SET LOCAL app.allow_ledger_delete = 'on';
--
-- UPDATE has no escape hatch. There is no legitimate reason to amend a ledger
-- row: a correction is a new, compensating entry, which is what a ledger is for.
CREATE FUNCTION fn_ledger_entry_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            'ledger_entry is append-only: the effect of event % cannot be amended',
            OLD.event_id
            USING ERRCODE = 'integrity_constraint_violation',
                  HINT = 'post a compensating entry instead of rewriting history';
    END IF;

    IF coalesce(current_setting('app.allow_ledger_delete', true), 'off') <> 'on' THEN
        RAISE EXCEPTION
            'ledger_entry is append-only: refusing to delete the effect of event %',
            OLD.event_id
            USING ERRCODE = 'integrity_constraint_violation',
                  HINT = 'archive first, then SET LOCAL app.allow_ledger_delete = ''on''';
    END IF;

    RETURN OLD;
END;
$$;
--@@
-- The attempt log is an audit trail, and an audit trail that can be edited is a
-- rumour. UPDATE is refused outright.
--
-- DELETE is allowed, unlike the ledger, and the difference is deliberate: an
-- attempt row is a *diagnostic*, a ledger row is *money*. Diagnostics have a
-- retention policy (see sp_purge_history); money does not.
CREATE FUNCTION fn_processing_attempt_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'processing_attempt is an audit log: attempt % of event % cannot be rewritten',
        OLD.attempt_number, OLD.event_id
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$;
--@@
-- `resolved` and `discarded` are terminal (FR-15, domain/dlq.py). The application
-- enforces this and returns a 409. This trigger enforces it against everything the
-- application is not: a migration script, an ad-hoc UPDATE, a future service.
--
-- An entry a human has ruled on must not quietly reopen. If the same event fails
-- again that is a NEW failure and deserves a new decision -- not a resurrection
-- that makes the history lie about what was known when.
CREATE FUNCTION fn_dlq_terminal_is_terminal() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IN ('resolved', 'discarded') AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION
            'dead_letter_entry % is %, which is terminal; it cannot become %',
            OLD.id, OLD.status, NEW.status
            USING ERRCODE = 'integrity_constraint_violation',
                  HINT = 'a new failure of the same event gets a new decision, not this one';
    END IF;
    RETURN NEW;
END;
$$;
"""

TRIGGERS = """
CREATE TRIGGER trg_account_set_updated_at
    BEFORE UPDATE ON account
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();
--@@
CREATE TRIGGER trg_ledger_entry_immutable
    BEFORE UPDATE OR DELETE ON ledger_entry
    FOR EACH ROW EXECUTE FUNCTION fn_ledger_entry_immutable();
--@@
CREATE TRIGGER trg_processing_attempt_immutable
    BEFORE UPDATE ON processing_attempt
    FOR EACH ROW EXECUTE FUNCTION fn_processing_attempt_immutable();
--@@
CREATE TRIGGER trg_dlq_terminal_is_terminal
    BEFORE UPDATE ON dead_letter_entry
    FOR EACH ROW EXECUTE FUNCTION fn_dlq_terminal_is_terminal();
"""


# --- Views: the operator's read model ----------------------------------------
#
# NFR-6: not one of these exposes `payload` or `headers`. A view is the easiest
# possible way to leak a body into a support ticket, so the omission is deliberate
# and load-bearing rather than an oversight.

VIEWS = """
-- "Are we falling behind?" -- the single view to look at first.
--
-- `waiting_on_backoff` is kept apart from `due_now` on purpose. A thousand pending
-- events are not a problem if they are all sitting out a retry delay; a thousand
-- *due* events mean the workers cannot keep up, and those are completely different
-- incidents that a single "pending" count would blur into one.
CREATE VIEW v_queue_health AS
SELECT
    count(*) FILTER (WHERE status = 'pending')                                AS pending,
    count(*) FILTER (WHERE status = 'pending' AND next_attempt_at <= now())   AS due_now,
    count(*) FILTER (WHERE status = 'pending' AND next_attempt_at >  now())   AS waiting_on_backoff,
    count(*) FILTER (WHERE status = 'succeeded')                              AS succeeded,
    count(*) FILTER (WHERE status = 'dead_lettered')                          AS dead_lettered,
    coalesce(
        max(now() - next_attempt_at) FILTER (WHERE status = 'pending' AND next_attempt_at <= now()),
        interval '0'
    )                                                                          AS oldest_due_age
FROM webhook_event;
--@@
-- THE invariant, as a query (NFR-1).
--
-- `drift` must be zero on every row, always. It is the difference between what the
-- account *says* its balance is and what the ledger can *prove* -- and if the two
-- ever disagree, an event was applied twice, or an effect was applied without a
-- ledger row, or somebody edited the balance by hand. Every correctness claim this
-- service makes reduces to `SELECT * FROM v_account_reconciliation WHERE drift <> 0`
-- coming back empty.
--
-- This is what the Day 4 load test asserts against after 10,000 duplicate
-- deliveries, and it is why the balance is a cached column rather than the truth:
-- the ledger is the truth, and the cache is checkable.
CREATE VIEW v_account_reconciliation AS
SELECT
    a.id,
    a.external_ref,
    a.balance_minor,
    a.version,
    coalesce(sum(l.amount_minor), 0)                    AS ledger_sum,
    a.balance_minor - coalesce(sum(l.amount_minor), 0)  AS drift,
    count(l.id)                                         AS ledger_rows
FROM account a
LEFT JOIN ledger_entry l ON l.account_id = a.id
GROUP BY a.id, a.external_ref, a.balance_minor, a.version;
--@@
-- The dead-letter queue an operator actually works from: everything still open,
-- oldest first, with the event context that answers "what broke, and whose money
-- is it?" without a join.
CREATE VIEW v_dlq_open AS
SELECT
    d.id,
    d.event_id,
    e.source,
    e.external_id,
    e.event_type,
    e.entity_type,
    e.entity_id,
    d.reason,
    d.attempts_made,
    d.status,
    d.dead_lettered_at,
    now() - d.dead_lettered_at AS age
FROM dead_letter_entry d
JOIN webhook_event e ON e.id = d.event_id
WHERE d.status IN ('needs_review', 'replaying')
ORDER BY d.dead_lettered_at;
--@@
-- One row per event, with the two questions the event row alone cannot answer:
-- how did its last attempt go, and did it actually produce an effect?
--
-- `has_effect` is the interesting column. An event that is `succeeded` with
-- `has_effect = false` is not a bug -- it is a superseded event (FR-10), correctly
-- handled and correctly not applied. Being able to see that at a glance is the
-- difference between a five-minute triage and an afternoon.
CREATE VIEW v_event_overview AS
SELECT
    e.id,
    e.source,
    e.external_id,
    e.event_type,
    e.entity_type,
    e.entity_id,
    e.status,
    e.attempt_count,
    e.received_at,
    e.occurred_at,
    e.next_attempt_at,
    e.processed_at,
    e.last_error,
    (SELECT count(*) FROM processing_attempt a WHERE a.event_id = e.id)   AS attempts_recorded,
    (SELECT max(a.finished_at) FROM processing_attempt a WHERE a.event_id = e.id)
                                                                          AS last_attempt_at,
    (SELECT a.outcome FROM processing_attempt a
      WHERE a.event_id = e.id ORDER BY a.attempt_number DESC LIMIT 1)     AS last_outcome,
    EXISTS (SELECT 1 FROM ledger_entry l WHERE l.event_id = e.id)         AS has_effect
FROM webhook_event e;
--@@
-- Where the time goes, and what fails. The percentile is computed by Postgres over
-- the real distribution rather than estimated from a histogram, which makes this
-- the thing to check the Prometheus buckets *against* when they disagree.
CREATE VIEW v_processing_outcomes AS
SELECT
    e.event_type,
    a.outcome,
    count(*)                                                            AS attempts,
    round(avg(a.duration_ms))::bigint                                   AS avg_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY a.duration_ms)::bigint AS p95_ms,
    max(a.duration_ms)                                                  AS max_ms
FROM processing_attempt a
JOIN webhook_event e ON e.id = a.event_id
GROUP BY e.event_type, a.outcome;
"""


# --- Functions ---------------------------------------------------------------

FUNCTIONS = """
-- The balance an account can *prove*, from the ledger, ignoring the cached column.
-- Use it to check `account.balance_minor`, never as a substitute for it: this is
-- O(rows) and the cache is O(1), which is the whole reason the cache exists.
CREATE FUNCTION fn_account_balance(p_external_ref text) RETURNS bigint
LANGUAGE sql STABLE AS $$
    SELECT coalesce(sum(l.amount_minor), 0)::bigint
    FROM ledger_entry l
    JOIN account a ON a.id = l.account_id
    WHERE a.external_ref = p_external_ref;
$$;
--@@
-- NFR-1 reduced to a single boolean. False means an effect was applied twice, or
-- applied without a ledger row, or a balance was edited by hand -- and every one of
-- those is an incident. Cheap enough to assert in a load test and in a smoke test.
CREATE FUNCTION fn_ledger_invariant_ok() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT NOT EXISTS (SELECT 1 FROM v_account_reconciliation WHERE drift <> 0);
$$;
--@@
-- How far behind the workers are, right now: the age of the oldest event that is
-- due and still waiting. This -- not the pending count -- is the number to alert
-- on, because it is the one the provider would notice.
CREATE FUNCTION fn_queue_lag() RETURNS interval
LANGUAGE sql STABLE AS $$
    SELECT coalesce(max(now() - next_attempt_at), interval '0')
    FROM webhook_event
    WHERE status = 'pending' AND next_attempt_at <= now();
$$;
"""


# --- Procedure: data lifecycle (NFR-12) --------------------------------------

PROCEDURE = """
-- Retention. A set operation over millions of rows, which is exactly what a
-- database is for and exactly what an ORM loop is not.
--
-- The care here is in what it REFUSES to delete.
--
-- `ledger_entry.event_id` is ON DELETE CASCADE, so deleting an old `webhook_event`
-- would take its ledger row with it -- and the account's balance would no longer
-- equal SUM(ledger_entry), breaking the invariant this whole service exists to
-- uphold. The cascade is right for a schema with no data older than a test, and it
-- is a loaded gun the moment a retention policy exists.
--
-- So this procedure only ever deletes events that produced NO effect: duplicates,
-- superseded snapshots, events whose handler was a no-op. Anything with a ledger
-- row stays, forever, until somebody writes a real archival step -- and if this
-- procedure is ever wrong about that, the append-only trigger on ledger_entry
-- stops it dead rather than letting it quietly destroy the audit trail.
--
-- Dead-lettered events keep their full attempt history regardless of age: they are
-- the ones a human still needs to read.
CREATE PROCEDURE sp_purge_history(
    p_older_than           interval DEFAULT interval '90 days',
    INOUT p_attempts_purged bigint  DEFAULT 0,
    INOUT p_events_purged   bigint  DEFAULT 0
)
LANGUAGE plpgsql AS $$
DECLARE
    v_cutoff timestamptz := now() - p_older_than;
BEGIN
    -- Diagnostics for events that ended well. The attempt log for a succeeded
    -- event is of no interest once it is old; the one for a dead-lettered event is
    -- the only thing an operator has to go on, so it is untouched.
    WITH purged AS (
        DELETE FROM processing_attempt a
        USING webhook_event e
        WHERE a.event_id = e.id
          AND e.status = 'succeeded'
          AND e.processed_at < v_cutoff
        RETURNING a.id
    )
    SELECT count(*) INTO p_attempts_purged FROM purged;

    -- Events that produced no effect, and therefore have nothing to protect.
    WITH purged AS (
        DELETE FROM webhook_event e
        WHERE e.status = 'succeeded'
          AND e.processed_at < v_cutoff
          AND NOT EXISTS (SELECT 1 FROM ledger_entry l WHERE l.event_id = e.id)
        RETURNING e.id
    )
    SELECT count(*) INTO p_events_purged FROM purged;

    RAISE NOTICE 'purged % attempt rows and % effectless events older than %',
        p_attempts_purged, p_events_purged, v_cutoff;
END;
$$;
"""


# --- What is deliberately absent ---------------------------------------------
#
# 1. A TRIGGER THAT MAINTAINS `account.balance_minor` FROM LEDGER INSERTS.
#    Tempting: it would make `balance == SUM(ledger)` true by construction rather
#    than by discipline. Refused, because the application already does exactly this
#    (adapters/ledger.py) in the same transaction as the insert -- so a trigger
#    would DOUBLE-APPLY every effect unless that code were removed, and the effect
#    would then live half in Python and half in a trigger nobody reviews. One
#    effect, one implementation, one place to look. `v_account_reconciliation`
#    gives us the guarantee's *verification* without splitting its *authorship*.
#
# 2. A SQL VERSION OF THE ADVISORY-LOCK KEY.
#    ADR-0002 derives it with blake2b in Python precisely so it cannot drift. There
#    is no blake2b in pgcrypto, so a SQL implementation would have to use a
#    different hash -- and a second key derivation that disagrees with the first
#    would take a *different* lock for the same account, serialise nothing, and
#    corrupt a balance in complete silence. This is the single most dangerous
#    function that could be added to this database, and it is not here.
#
# 3. A STORED PROCEDURE THAT CLAIMS AND PROCESSES EVENTS.
#    The FOR UPDATE SKIP LOCKED claim, the advisory lock, and the dispatch are
#    tested against a real Postgres from Python (tests/integration/). Moving them
#    into plpgsql would move them out of the type checker, out of the test suite's
#    reach, and out of code review, to buy a round-trip we are not short of.
#
# 4. A TRIGGER GUARDING `webhook_event.status` TRANSITIONS.
#    It looks like an obvious companion to the DLQ guard -- and it would break
#    replay. Replay legitimately moves an event from a terminal state (`succeeded`,
#    `dead_lettered`) back to `pending`; that is the whole feature (FR-16). A
#    "terminal is terminal" rule is correct for a DLQ entry, which records a human's
#    decision, and wrong for an event, which records a fact that can be re-derived.


def _statements(script: str) -> list[str]:
    """Split a script into individual statements.

    asyncpg refuses more than one command per prepared statement, so each CREATE
    has to be sent on its own. Splitting on `;` would be wrong -- every plpgsql
    body is full of them -- so the scripts are separated by a marker line instead,
    which keeps the SQL above readable as SQL.
    """
    return [chunk.strip() for chunk in script.split("\n--@@\n") if chunk.strip()]


def upgrade() -> None:
    for script in (TRIGGER_FUNCTIONS, TRIGGERS, VIEWS, FUNCTIONS, PROCEDURE):
        for statement in _statements(script):
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP PROCEDURE IF EXISTS sp_purge_history(interval, bigint, bigint)")

    op.execute("DROP FUNCTION IF EXISTS fn_queue_lag()")
    op.execute("DROP FUNCTION IF EXISTS fn_ledger_invariant_ok()")
    op.execute("DROP FUNCTION IF EXISTS fn_account_balance(text)")

    # Views before the trigger functions, and `v_account_reconciliation` last of the
    # views: `fn_ledger_invariant_ok` depends on it, and it is already gone above.
    op.execute("DROP VIEW IF EXISTS v_processing_outcomes")
    op.execute("DROP VIEW IF EXISTS v_event_overview")
    op.execute("DROP VIEW IF EXISTS v_dlq_open")
    op.execute("DROP VIEW IF EXISTS v_account_reconciliation")
    op.execute("DROP VIEW IF EXISTS v_queue_health")

    op.execute("DROP TRIGGER IF EXISTS trg_dlq_terminal_is_terminal ON dead_letter_entry")
    op.execute("DROP TRIGGER IF EXISTS trg_processing_attempt_immutable ON processing_attempt")
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_entry_immutable ON ledger_entry")
    op.execute("DROP TRIGGER IF EXISTS trg_account_set_updated_at ON account")

    op.execute("DROP FUNCTION IF EXISTS fn_dlq_terminal_is_terminal()")
    op.execute("DROP FUNCTION IF EXISTS fn_processing_attempt_immutable()")
    op.execute("DROP FUNCTION IF EXISTS fn_ledger_entry_immutable()")
    op.execute("DROP FUNCTION IF EXISTS fn_set_updated_at()")
