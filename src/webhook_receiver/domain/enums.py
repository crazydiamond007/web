"""State enums for the event lifecycle (SPEC §3).

Pure Python. The domain layer never imports the framework (SPEC §4), so these
carry no SQLAlchemy or Pydantic dependency; the persistence adapter maps them
onto Postgres enum types.
"""

from __future__ import annotations

from enum import StrEnum


class WebhookStatus(StrEnum):
    """Lifecycle of a `webhook_event` row.

    There is deliberately no `retrying` state. A retryable failure returns the
    event to `PENDING` with `next_attempt_at` set in the future, so the worker's
    poll predicate is a single condition -- `status = 'pending' AND
    next_attempt_at <= now()` -- served by one index for both first attempts and
    retries (SPEC §3, FR-7, FR-12).
    """

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    DEAD_LETTERED = "dead_lettered"


class AttemptOutcome(StrEnum):
    """How one `processing_attempt` ended.

    `SUPERSEDED` extends the enum listed in SPEC §3. FR-10 requires that a stale
    event be "recorded as superseded rather than applied", but none of the three
    outcomes the spec names can express that: the attempt did not fail, and it
    did not apply an effect either. Encoding it as `SUCCEEDED` would make the
    ledger and the attempt log disagree about whether an effect exists.

    Postgres cannot add an enum value inside a transaction without care, so the
    value is introduced in the initial migration rather than bolted on later.
    See docs/adr/0006-superseded-attempt-outcome.md.
    """

    SUCCEEDED = "succeeded"
    SUPERSEDED = "superseded"
    RETRYABLE_ERROR = "retryable_error"
    NON_RETRYABLE_ERROR = "non_retryable_error"


class DlqStatus(StrEnum):
    """Triage state of a `dead_letter_entry` (FR-15)."""

    NEEDS_REVIEW = "needs_review"
    REPLAYING = "replaying"
    RESOLVED = "resolved"
    DISCARDED = "discarded"


class ReplayOutcome(StrEnum):
    """Result of a `replay_request` (FR-16, FR-17).

    `SKIPPED_ALREADY_PROCESSED` is the expected outcome of replaying an event
    whose effect already exists -- the guarantee FR-17 asks us to prove.
    """

    SUCCEEDED = "succeeded"
    SKIPPED_ALREADY_PROCESSED = "skipped_already_processed"
    FAILED = "failed"
