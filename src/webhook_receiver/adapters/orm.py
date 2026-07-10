"""SQLAlchemy mapping of the schema in SPEC §3.

The constraints in this module are the correctness mechanism, not decoration.
Read the four `UniqueConstraint`s as the specification of the system:

* ``uq_webhook_event_source_idempotency_key`` -- deduplication (FR-5).
* ``uq_ledger_entry_event_id``                -- idempotent effect (FR-6).
* ``uq_processing_attempt_event_id_attempt_number``
* ``uq_dead_letter_entry_event_id``           -- one DLQ entry per event.

Nothing in the application layer performs a SELECT-then-INSERT to enforce any of
these. The insert is attempted and Postgres arbitrates, atomically, under
concurrency. That is the whole design.

Schema changes go through Alembic; the application never calls `create_all`
(SPEC §6.1). `Base.metadata` exists here only so migrations can diff against it.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from webhook_receiver.domain.enums import (
    AttemptOutcome,
    DlqStatus,
    ReplayOutcome,
    WebhookStatus,
)

# A JSON object as stored in JSONB. `object` rather than `Any`: callers must
# narrow before use, which is honest about what came off the wire (SPEC §6.2).
type JsonObject = dict[str, object]

# Deterministic constraint names, so Alembic autogenerate produces stable diffs
# and a failing constraint is greppable from a production log line.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _pg_enum(
    enum_cls: type[WebhookStatus | AttemptOutcome | DlqStatus | ReplayOutcome], name: str
) -> Enum:
    """Map a StrEnum onto a native Postgres enum, storing values not member names.

    Without `values_callable`, SQLAlchemy persists `PENDING` rather than
    `pending`, silently diverging from the schema in SPEC §3.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # ClassVar is inherited from DeclarativeBase, but ruff cannot see through
    # SQLAlchemy's declarative machinery to know that (RUF012). Restating it is
    # cheaper than a noqa and keeps the mutability intent explicit.
    type_annotation_map: ClassVar[dict[object, object]] = {
        JsonObject: JSONB,
        datetime: DateTime(timezone=True),
    }


def _pk() -> Mapped[int]:
    """`bigint` identity primary key, per SPEC §3."""
    return mapped_column(BigInteger, Identity(always=False), primary_key=True)


class WebhookEvent(Base):
    """A delivery we have durably accepted (FR-1, NFR-3).

    The row is written before the 200 goes out. `status`/`next_attempt_at` make
    it a work queue; `(source, idempotency_key)` makes redelivery a no-op.
    """

    __tablename__ = "webhook_event"
    __table_args__ = (
        # FR-5: THE deduplication guarantee. A repeat delivery loses the race
        # here rather than in application code.
        UniqueConstraint(
            "source", "idempotency_key", name="uq_webhook_event_source_idempotency_key"
        ),
        # FR-7: the worker poll predicate, `status = 'pending' AND next_attempt_at <= now()`.
        Index("ix_webhook_event_status_next_attempt_at", "status", "next_attempt_at"),
        # FR-9 / FR-18: locate an entity's events for serialisation and for the admin API.
        Index("ix_webhook_event_entity_type_entity_id", "entity_type", "entity_id"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
    )

    id: Mapped[int] = _pk()

    source: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)

    payload: Mapped[JsonObject] = mapped_column(JSONB)
    headers: Mapped[JsonObject] = mapped_column(JSONB)
    signature_verified: Mapped[bool] = mapped_column(Boolean)

    # FR-10: ordering inputs. `provider_sequence` is nullable because not every
    # provider supplies one; `occurred_at` is the fallback ordering key.
    occurred_at: Mapped[datetime]
    provider_sequence: Mapped[int | None] = mapped_column(BigInteger)

    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    status: Mapped[WebhookStatus] = mapped_column(
        _pg_enum(WebhookStatus, "webhook_status"),
        server_default=WebhookStatus.PENDING.value,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    # NOT NULL with a `now()` default: a fresh event is immediately due, so the
    # poll predicate needs no COALESCE and the index stays usable.
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)


class ProcessingAttempt(Base):
    """One pass of a worker over one event (NFR-5: traceable from the tables alone)."""

    __tablename__ = "processing_attempt"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "attempt_number", name="uq_processing_attempt_event_id_attempt_number"
        ),
        Index("ix_processing_attempt_event_id", "event_id"),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
    )

    id: Mapped[int] = _pk()
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("webhook_event.id", ondelete="CASCADE")
    )
    attempt_number: Mapped[int] = mapped_column(Integer)

    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Nullable until the attempt lands. A row with `finished_at IS NULL` and a
    # stale `started_at` is exactly the signature of a worker that died
    # mid-processing (NFR-4).
    finished_at: Mapped[datetime | None]
    outcome: Mapped[AttemptOutcome | None] = mapped_column(
        _pg_enum(AttemptOutcome, "attempt_outcome")
    )
    error_class: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class DeadLetterEntry(Base):
    """An event that exhausted retries or failed non-retryably (FR-14, FR-15)."""

    __tablename__ = "dead_letter_entry"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_dead_letter_entry_event_id"),
        Index("ix_dead_letter_entry_status", "status"),
        CheckConstraint("attempts_made >= 0", name="attempts_made_non_negative"),
    )

    id: Mapped[int] = _pk()
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("webhook_event.id", ondelete="CASCADE")
    )
    reason: Mapped[str] = mapped_column(Text)
    attempts_made: Mapped[int] = mapped_column(Integer)
    dead_lettered_at: Mapped[datetime] = mapped_column(server_default=func.now())
    status: Mapped[DlqStatus] = mapped_column(
        _pg_enum(DlqStatus, "dlq_status"),
        server_default=DlqStatus.NEEDS_REVIEW.value,
    )
    resolved_at: Mapped[datetime | None]
    resolution_note: Mapped[str | None] = mapped_column(Text)


class ReplayRequest(Base):
    """An audited operator-initiated reprocess (FR-16: who, when, why, and how it went)."""

    __tablename__ = "replay_request"
    __table_args__ = (Index("ix_replay_request_event_id", "event_id"),)

    id: Mapped[int] = _pk()
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("webhook_event.id", ondelete="CASCADE")
    )
    requested_by: Mapped[str] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    reason: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[ReplayOutcome | None] = mapped_column(_pg_enum(ReplayOutcome, "replay_outcome"))
    # SET NULL, not CASCADE: losing an attempt row must not erase the audit
    # record that somebody asked for a replay.
    resulting_attempt_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("processing_attempt.id", ondelete="SET NULL")
    )


class Account(Base):
    """Demo business entity. `version` backs the optimistic guard for FR-10."""

    __tablename__ = "account"
    __table_args__ = (
        UniqueConstraint("external_ref", name="uq_account_external_ref"),
        CheckConstraint("version >= 0", name="version_non_negative"),
    )

    id: Mapped[int] = _pk()
    external_ref: Mapped[str] = mapped_column(Text)
    balance_minor: Mapped[int] = mapped_column(BigInteger, server_default="0")
    version: Mapped[int] = mapped_column(Integer, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LedgerEntry(Base):
    """The business effect. One row per event, forever (FR-6).

    `uq_ledger_entry_event_id` is what makes reprocessing safe: a second attempt
    to apply the same event raises a unique violation instead of moving money
    twice. The load test asserts `count(ledger_entry) == count(distinct events)`
    against this constraint (NFR-1).
    """

    __tablename__ = "ledger_entry"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_ledger_entry_event_id"),
        Index("ix_ledger_entry_account_id", "account_id"),
    )

    id: Mapped[int] = _pk()
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="RESTRICT")
    )
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("webhook_event.id", ondelete="CASCADE")
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
