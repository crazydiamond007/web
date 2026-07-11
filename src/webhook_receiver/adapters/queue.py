"""The event table used as a work queue (FR-7, FR-13, FR-14).

`webhook_event` is both the record of what arrived and the queue of what is left
to do. SPEC §4 and ADR-0001 explain why that is one table and not two: the effect
and the queue state then commit in the *same transaction*, which is what makes
NFR-4 hold without an outbox or a distributed transaction.

The claim is `SELECT ... FOR UPDATE SKIP LOCKED`. `SKIP LOCKED` is the whole
reason this works with more than one worker: without it, the second worker's
`SELECT ... FOR UPDATE` would *block* on the first worker's row rather than move
past it, and N workers would process events one at a time while looking, from the
outside, like a fleet.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from webhook_receiver.adapters.orm import (
    DeadLetterEntry,
    ProcessingAttempt,
    ReplayRequest,
    WebhookEvent,
)
from webhook_receiver.domain.enums import AttemptOutcome, DlqStatus, ReplayOutcome, WebhookStatus
from webhook_receiver.domain.events import StoredEvent

DLQ_CONSTRAINT = "uq_dead_letter_entry_event_id"


def _to_domain(row: WebhookEvent) -> StoredEvent:
    return StoredEvent(
        id=row.id,
        source=row.source,
        event_type=row.event_type,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        payload=row.payload,
        occurred_at=row.occurred_at,
        provider_sequence=row.provider_sequence,
        attempt_count=row.attempt_count,
    )


async def due_event_ids(session: AsyncSession, *, now: datetime, limit: int) -> Sequence[int]:
    """Ids of events that are ready to run, oldest deadline first.

    An unlocked read, on purpose. It is a *hint*, not a claim: by the time we act
    on an id another worker may have taken it, which is exactly why `claim` below
    re-checks the predicate under a lock. Trying to make this read authoritative
    would mean holding a transaction open across the whole batch, and then one
    slow handler would keep the other events in the batch hostage.
    """
    statement = (
        select(WebhookEvent.id)
        .where(
            WebhookEvent.status == WebhookStatus.PENDING,
            WebhookEvent.next_attempt_at <= now,
        )
        # Oldest deadline first, so a retried event does not starve behind a
        # steady arrival of new ones. Served by ix_webhook_event_status_next_attempt_at.
        .order_by(WebhookEvent.next_attempt_at)
        .limit(limit)
    )
    return (await session.execute(statement)).scalars().all()


async def claim(session: AsyncSession, *, event_id: int, now: datetime) -> StoredEvent | None:
    """Take exclusive ownership of one event for the life of this transaction.

    Returns `None` when another worker holds the row (`SKIP LOCKED` steps over
    it) or when it is no longer due -- both mean "not ours", and both are normal
    rather than exceptional.

    The predicate is re-checked here even though `due_event_ids` already checked
    it. That is not redundancy: between the two statements another worker can
    have finished this very event, and the row lock is the only thing that makes
    the check meaningful.
    """
    statement = (
        select(WebhookEvent)
        .where(
            WebhookEvent.id == event_id,
            WebhookEvent.status == WebhookStatus.PENDING,
            WebhookEvent.next_attempt_at <= now,
        )
        .with_for_update(skip_locked=True)
    )
    row = (await session.execute(statement)).scalar_one_or_none()
    return None if row is None else _to_domain(row)


async def load_for_update(session: AsyncSession, *, event_id: int) -> StoredEvent | None:
    """Re-take one event by id, waiting rather than skipping.

    Used only by the failure path, which runs in a *fresh* transaction after the
    processing transaction rolled back (see `services/process.py`). Here we
    genuinely need the row -- skipping it would mean losing the record that the
    attempt happened -- so this blocks instead of using `SKIP LOCKED`.
    """
    statement = select(WebhookEvent).where(WebhookEvent.id == event_id).with_for_update()
    row = (await session.execute(statement)).scalar_one_or_none()
    return None if row is None else _to_domain(row)


async def next_attempt_number(session: AsyncSession, *, event_id: int) -> int:
    """The next unused `attempt_number` for this event.

    Derived from the attempt *history*, not from `attempt_count`, because the two
    stopped meaning the same thing the moment replay existed (FR-16):

    * `attempt_number` is a fact about what happened -- it must never repeat, or
      `uq_processing_attempt_event_id_attempt_number` rejects the row and the
      audit trail loses an attempt.
    * `attempt_count` is the retry *budget*, and a replay deliberately resets it
      so a fixed event gets a fresh set of tries (FR-13).

    Deriving one from the other, as this did before replay landed, meant that
    replaying any event with a prior attempt would violate the unique constraint.
    """
    highest = (
        await session.execute(
            select(func.max(ProcessingAttempt.attempt_number)).where(
                ProcessingAttempt.event_id == event_id
            )
        )
    ).scalar_one_or_none()
    return (highest or 0) + 1


async def requeue(session: AsyncSession, *, event_id: int, now: datetime) -> None:
    """Make an event due again, with a clean retry budget (FR-16).

    Used only by replay. `attempt_count` goes back to zero on purpose: an operator
    replaying an event has usually just fixed whatever broke it, and making it
    fail once and dead-letter itself immediately -- because the old budget was
    already spent -- would make replay useless for the case it exists to serve.

    The attempt *history* is untouched. It is an audit log, and an audit log that
    forgets is not one.
    """
    await session.execute(
        update(WebhookEvent)
        .where(WebhookEvent.id == event_id)
        .values(
            status=WebhookStatus.PENDING,
            attempt_count=0,
            next_attempt_at=now,
            last_error=None,
            processed_at=None,
        )
    )


async def record_attempt(
    session: AsyncSession,
    *,
    event_id: int,
    attempt_number: int,
    started_at: datetime,
    finished_at: datetime,
    outcome: AttemptOutcome,
    error_class: str | None = None,
    error_detail: str | None = None,
) -> None:
    """Write the audit row for one pass over one event (NFR-5).

    `uq_processing_attempt_event_id_attempt_number` means a double-write of the
    same attempt is a constraint violation rather than a duplicated history.
    """
    duration = finished_at - started_at
    session.add(
        ProcessingAttempt(
            event_id=event_id,
            attempt_number=attempt_number,
            started_at=started_at,
            finished_at=finished_at,
            outcome=outcome,
            error_class=error_class,
            error_detail=error_detail,
            duration_ms=int(duration.total_seconds() * 1000),
        )
    )
    await session.flush()


async def mark_succeeded(
    session: AsyncSession, *, event_id: int, attempts_used: int, now: datetime
) -> None:
    """Terminal, and it includes the superseded case.

    A superseded event *is* fully handled -- we looked at it and correctly decided
    it must not be applied. The nuance belongs on the attempt row, not on the
    event: the event is done, and re-running it would only reach the same
    conclusion. See ADR-0006.
    """
    await session.execute(
        update(WebhookEvent)
        .where(WebhookEvent.id == event_id)
        .values(
            status=WebhookStatus.SUCCEEDED,
            # The retry budget consumed in this cycle -- not the attempt number,
            # which counts the whole history including previous replays.
            attempt_count=attempts_used,
            processed_at=now,
            last_error=None,
        )
    )


async def reschedule(
    session: AsyncSession,
    *,
    event_id: int,
    attempts_used: int,
    next_attempt_at: datetime,
    last_error: str,
) -> None:
    """Return a failed event to the queue with a future deadline (FR-12).

    Back to `PENDING`, never to a `retrying` state: the poll predicate stays the
    single condition `status = 'pending' AND next_attempt_at <= now()`, which one
    index serves for both first attempts and retries.
    """
    await session.execute(
        update(WebhookEvent)
        .where(WebhookEvent.id == event_id)
        .values(
            status=WebhookStatus.PENDING,
            attempt_count=attempts_used,
            next_attempt_at=next_attempt_at,
            last_error=last_error,
        )
    )


async def latest_attempt_id(session: AsyncSession, *, event_id: int) -> int | None:
    """The attempt a replay produced, for `replay_request.resulting_attempt_id`.

    `None` when the replay never got as far as an attempt -- another worker had
    already claimed the event. The audit row is written either way; a replay that
    achieved nothing is exactly the kind an operator goes looking for later.
    """
    return (
        await session.execute(
            select(func.max(ProcessingAttempt.id)).where(ProcessingAttempt.event_id == event_id)
        )
    ).scalar_one_or_none()


async def record_replay_request(
    session: AsyncSession,
    *,
    event_id: int,
    requested_by: str,
    reason: str | None,
    outcome: ReplayOutcome,
    requested_at: datetime,
    resulting_attempt_id: int | None,
) -> None:
    """Who ran the replay, when, why, and how it went (FR-16).

    Written unconditionally, including for a replay that failed or did nothing. An
    audit trail that only records the successes is a marketing document.
    """
    session.add(
        ReplayRequest(
            event_id=event_id,
            requested_by=requested_by,
            requested_at=requested_at,
            reason=reason,
            outcome=outcome,
            resulting_attempt_id=resulting_attempt_id,
        )
    )
    await session.flush()


async def dead_lettered_event_ids(
    session: AsyncSession, *, limit: int, dlq_status: DlqStatus = DlqStatus.NEEDS_REVIEW
) -> Sequence[int]:
    """Events sitting in the DLQ, for a "replay the DLQ" request (FR-16).

    Oldest first here, unlike the operator-facing listing: a bulk replay should
    drain the backlog in the order it accumulated, so a poison event from March
    does not sit behind everything that broke since.
    """
    statement = (
        select(DeadLetterEntry.event_id)
        .where(DeadLetterEntry.status == dlq_status)
        .order_by(DeadLetterEntry.dead_lettered_at)
        .limit(limit)
    )
    return (await session.execute(statement)).scalars().all()


async def event_ids_in_range(
    session: AsyncSession, *, since: datetime, until: datetime, limit: int
) -> Sequence[int]:
    """Events received in a time window, for a "replay this window" request (FR-16).

    On `received_at`, not `occurred_at`: an operator replaying a window is
    reasoning about when *we* got it and therefore when our bug could have touched
    it, not about when it happened at the provider.
    """
    statement = (
        select(WebhookEvent.id)
        .where(WebhookEvent.received_at >= since, WebhookEvent.received_at < until)
        .order_by(WebhookEvent.received_at)
        .limit(limit)
    )
    return (await session.execute(statement)).scalars().all()


async def dead_letter(
    session: AsyncSession,
    *,
    event_id: int,
    attempts_made: int,
    reason: str,
) -> None:
    """Stop retrying and hand the event to a human (FR-14).

    The status change and the DLQ row are written together, in the caller's
    transaction, so the invariant "every dead-lettered event has exactly one DLQ
    entry" cannot be broken by a crash between the two.

    `ON CONFLICT DO NOTHING` keeps this callable twice -- the replay path (FR-16)
    can re-dead-letter an event it failed to fix, and that must not raise.
    """
    await session.execute(
        update(WebhookEvent)
        .where(WebhookEvent.id == event_id)
        .values(
            status=WebhookStatus.DEAD_LETTERED,
            attempt_count=attempts_made,
            last_error=reason,
        )
    )
    await session.execute(
        insert(DeadLetterEntry)
        .values(event_id=event_id, reason=reason, attempts_made=attempts_made)
        .on_conflict_do_nothing(constraint=DLQ_CONSTRAINT)
    )
