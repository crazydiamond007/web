"""Reading and triaging the dead-letter queue (FR-14, FR-15).

The DLQ *rows* are written by `adapters/queue.dead_letter`, inside the same
transaction as the event's status change -- so the invariant "every dead-lettered
event has exactly one DLQ entry" is not something a background job maintains, it
is something a transaction guarantees.

What lives here is the other half: letting a human read the queue and act on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from webhook_receiver.adapters.orm import DeadLetterEntry, WebhookEvent
from webhook_receiver.domain.dlq import TERMINAL, ensure_transition
from webhook_receiver.domain.enums import DlqStatus


@dataclass(frozen=True, slots=True)
class DlqRecord:
    """One dead-lettered event, with enough context to triage it without a join.

    The event's `source`, `event_type` and `entity_id` are carried along because
    the first question an operator asks is never "what is entry 4172?" -- it is
    "what broke, and whose money is it?".
    """

    id: int
    event_id: int
    source: str
    event_type: str
    entity_type: str
    entity_id: str
    external_id: str
    reason: str
    attempts_made: int
    dead_lettered_at: datetime
    status: DlqStatus
    resolved_at: datetime | None
    resolution_note: str | None


def _base_query() -> Select[tuple[DeadLetterEntry, WebhookEvent]]:
    """Selecting the two entities, not a tuple of columns.

    `select(*columns)` erases the row type -- mypy sees `tuple[Any, ...]` and every
    field access downstream becomes unchecked. Selecting the mapped classes keeps
    the join fully typed all the way into `DlqRecord`.
    """
    return select(DeadLetterEntry, WebhookEvent).join(
        WebhookEvent, WebhookEvent.id == DeadLetterEntry.event_id
    )


def _to_record(entry: DeadLetterEntry, event: WebhookEvent) -> DlqRecord:
    return DlqRecord(
        id=entry.id,
        event_id=entry.event_id,
        source=event.source,
        event_type=event.event_type,
        entity_type=event.entity_type,
        entity_id=event.entity_id,
        external_id=event.external_id,
        reason=entry.reason,
        attempts_made=entry.attempts_made,
        dead_lettered_at=entry.dead_lettered_at,
        status=entry.status,
        resolved_at=entry.resolved_at,
        resolution_note=entry.resolution_note,
    )


async def list_entries(
    session: AsyncSession,
    *,
    status: DlqStatus | None = None,
    limit: int,
    offset: int = 0,
) -> Sequence[DlqRecord]:
    statement = _base_query()
    if status is not None:
        statement = statement.where(DeadLetterEntry.status == status)
    # Newest first: an operator opening the DLQ wants what just broke, not what
    # broke in March.
    statement = (
        statement.order_by(DeadLetterEntry.dead_lettered_at.desc()).limit(limit).offset(offset)
    )

    rows = (await session.execute(statement)).all()
    return [_to_record(entry, event) for entry, event in rows]


async def get_entry(session: AsyncSession, *, entry_id: int) -> DlqRecord | None:
    row = (await session.execute(_base_query().where(DeadLetterEntry.id == entry_id))).one_or_none()
    return None if row is None else _to_record(*row)


async def entry_for_event(session: AsyncSession, *, event_id: int) -> DlqRecord | None:
    statement = _base_query().where(DeadLetterEntry.event_id == event_id)
    row = (await session.execute(statement)).one_or_none()
    return None if row is None else _to_record(*row)


async def transition(
    session: AsyncSession,
    *,
    entry_id: int,
    target: DlqStatus,
    now: datetime,
    note: str | None = None,
) -> DlqRecord:
    """Move an entry to `target`, or refuse (FR-15).

    The current status is read `FOR UPDATE` so two operators clicking "resolve"
    and "discard" at the same moment cannot both win -- the second one waits, sees
    the terminal status the first one wrote, and is refused. A read-then-write
    without the lock would let both succeed and leave the loser's decision
    silently overwritten.
    """
    current = (
        await session.execute(
            select(DeadLetterEntry.status).where(DeadLetterEntry.id == entry_id).with_for_update()
        )
    ).scalar_one()

    ensure_transition(current, target)

    values: dict[str, object] = {"status": target, "resolution_note": note}
    # Only a terminal state has a resolution time. `replaying` is not an outcome,
    # it is a thing in progress.
    if target in TERMINAL:
        values["resolved_at"] = now

    await session.execute(
        update(DeadLetterEntry).where(DeadLetterEntry.id == entry_id).values(**values)
    )

    entry = await get_entry(session, entry_id=entry_id)
    if entry is None:  # pragma: no cover -- we hold a row lock on it
        msg = f"dead-letter entry {entry_id} vanished under a row lock"
        raise RuntimeError(msg)
    return entry
