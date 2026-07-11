"""The admin read model (FR-18).

Separate from `queue.py` on purpose. That module is the *write* side -- claim,
lock, finish -- and every statement in it is on the hot path of a worker. This one
is the *read* side: an operator asking what happened, at human speed, with filters
a worker would never use.

Keeping them apart means an index added for the admin API cannot slow the poll
loop by accident, and a query written for a support ticket cannot end up in the
worker's transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from webhook_receiver.adapters.orm import ProcessingAttempt, WebhookEvent
from webhook_receiver.domain.enums import AttemptOutcome, WebhookStatus


@dataclass(frozen=True, slots=True)
class EventSummary:
    id: int
    source: str
    external_id: str
    event_type: str
    entity_type: str
    entity_id: str
    status: WebhookStatus
    attempt_count: int
    occurred_at: datetime
    received_at: datetime
    next_attempt_at: datetime
    processed_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    id: int
    attempt_number: int
    started_at: datetime
    finished_at: datetime | None
    outcome: AttemptOutcome | None
    error_class: str | None
    error_detail: str | None
    duration_ms: int | None


@dataclass(frozen=True, slots=True)
class EventDetail:
    event: EventSummary
    attempts: Sequence[AttemptRecord]


def _summarise(row: WebhookEvent) -> EventSummary:
    # `payload` and `headers` are deliberately absent. The admin API is a support
    # tool, and a support tool that prints the raw payload turns every screenshot
    # in a ticket into a leak (NFR-6). Fetch the row from the database if you truly
    # need the body, and be deliberate about it.
    return EventSummary(
        id=row.id,
        source=row.source,
        external_id=row.external_id,
        event_type=row.event_type,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        status=row.status,
        attempt_count=row.attempt_count,
        occurred_at=row.occurred_at,
        received_at=row.received_at,
        next_attempt_at=row.next_attempt_at,
        processed_at=row.processed_at,
        last_error=row.last_error,
    )


def _filtered(
    *,
    status: WebhookStatus | None,
    source: str | None,
    event_type: str | None,
    entity_type: str | None,
    entity_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Select[tuple[WebhookEvent]]:
    statement = select(WebhookEvent)
    if status is not None:
        statement = statement.where(WebhookEvent.status == status)
    if source is not None:
        statement = statement.where(WebhookEvent.source == source)
    if event_type is not None:
        statement = statement.where(WebhookEvent.event_type == event_type)
    if entity_type is not None:
        statement = statement.where(WebhookEvent.entity_type == entity_type)
    if entity_id is not None:
        statement = statement.where(WebhookEvent.entity_id == entity_id)
    if since is not None:
        statement = statement.where(WebhookEvent.received_at >= since)
    if until is not None:
        statement = statement.where(WebhookEvent.received_at < until)
    return statement


async def list_events(
    session: AsyncSession,
    *,
    status: WebhookStatus | None = None,
    source: str | None = None,
    event_type: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int,
    offset: int = 0,
) -> Sequence[EventSummary]:
    """Filter events (FR-18). Newest first -- an operator is looking at *now*."""
    statement = _filtered(
        status=status,
        source=source,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        since=since,
        until=until,
    )
    statement = statement.order_by(WebhookEvent.received_at.desc()).limit(limit).offset(offset)

    rows = (await session.execute(statement)).scalars().all()
    return [_summarise(row) for row in rows]


async def get_event(session: AsyncSession, *, event_id: int) -> EventDetail | None:
    """One event with its full attempt history (FR-18).

    The history is the point. "Why is this event still pending?" is answered by
    the attempts -- three retryable failures with a growing backoff is a very
    different story from one non-retryable failure -- and neither is visible from
    the event row alone.
    """
    row = (
        await session.execute(select(WebhookEvent).where(WebhookEvent.id == event_id))
    ).scalar_one_or_none()
    if row is None:
        return None

    attempts = (
        await session.execute(
            select(ProcessingAttempt)
            .where(ProcessingAttempt.event_id == event_id)
            .order_by(ProcessingAttempt.attempt_number)
        )
    ).scalars()

    return EventDetail(
        event=_summarise(row),
        attempts=[
            AttemptRecord(
                id=attempt.id,
                attempt_number=attempt.attempt_number,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
                outcome=attempt.outcome,
                error_class=attempt.error_class,
                error_detail=attempt.error_detail,
                duration_ms=attempt.duration_ms,
            )
            for attempt in attempts
        ],
    )
