"""Processing one event: claim, lock, dispatch, apply, record (FR-7..FR-10).

The shape of this file is one decision, and it is the important one:

    **Everything about one event commits in a single transaction, or none of it
    does.** The claim, the advisory lock, the ledger row, the balance, the attempt
    record and the status change are one atomic unit.

That is what makes NFR-4 -- "a worker that dies mid-processing leaves no
half-applied effect" -- true by construction rather than by care. Kill the worker
at any instant and Postgres rolls the whole thing back: the ledger row vanishes
with the balance change that matched it, the event returns to `pending`, and the
advisory lock dies with the connection. There is no reaper, no lease, no
`processing` row to sweep up, and no state that can be left inconsistent, because
there is no moment at which it exists. See ADR-0003.

The failure path is the subtle part. Once a *database* statement has failed, the
transaction is aborted and Postgres will refuse every further statement in it --
so the failure cannot be recorded in the transaction that failed. The bookkeeping
therefore runs in a second, fresh transaction, after the first has rolled back.
This is the reason `process_event` takes a session *factory* and not a session.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from webhook_receiver.adapters import queue
from webhook_receiver.adapters.clock import Clock
from webhook_receiver.adapters.database import session_scope
from webhook_receiver.adapters.ledger import apply_effect
from webhook_receiver.adapters.locks import lock_entity
from webhook_receiver.config import Settings
from webhook_receiver.domain.effects import EffectResult
from webhook_receiver.domain.enums import AttemptOutcome
from webhook_receiver.domain.errors import NonRetryableError, ProcessingError
from webhook_receiver.domain.events import StoredEvent
from webhook_receiver.domain.handlers import HandlerRegistry

log = structlog.get_logger(__name__)

# How much of an error message we keep in `last_error` and `processing_attempt`.
# A display bound, not a tunable policy: SQLAlchemy renders the whole failing
# statement into its exception, and an unbounded string in a column an operator
# greps is a nuisance, not a feature. Parameters are already suppressed by the
# engine (`hide_parameters`), so no payload can reach here (NFR-6).
ERROR_DETAIL_LIMIT = 500

_SUCCESS = {
    EffectResult.APPLIED: AttemptOutcome.SUCCEEDED,
    # The effect already existed, so this pass did exactly what it should: nothing.
    # It is a success, and the ledger proves it (FR-6).
    EffectResult.ALREADY_APPLIED: AttemptOutcome.SUCCEEDED,
    # Neither a success nor a failure: we looked, and correctly declined to apply
    # it. The fourth outcome exists so the attempt log and the ledger cannot
    # disagree about whether an effect happened (FR-10, ADR-0006).
    EffectResult.SUPERSEDED: AttemptOutcome.SUPERSEDED,
}


async def process_event(
    factory: async_sessionmaker[AsyncSession],
    *,
    event_id: int,
    registry: HandlerRegistry,
    settings: Settings,
    clock: Clock,
) -> AttemptOutcome | None:
    """Run one event to a terminal state, or hand it back.

    Returns the attempt's outcome, or `None` when the event was not ours to
    process -- another worker holds it, or it is no longer due. That is a normal
    result of two workers polling the same queue, not an error.
    """
    started_at = clock.now()
    try:
        async with session_scope(factory) as session:
            event = await queue.claim(session, event_id=event_id, now=started_at)
            if event is None:
                return None
            return await _apply(
                session,
                event=event,
                registry=registry,
                settings=settings,
                clock=clock,
                started_at=started_at,
            )
    except Exception as exc:  # noqa: BLE001
        # The one deliberate catch-all in the codebase (SPEC §6.6 forbids the
        # rest). It is here because *any* escaping exception kills the worker
        # loop, and a single poison event must not take the fleet down with it.
        #
        # Anything not explicitly NonRetryable is treated as retryable, including
        # a bug in our own code: an unknown failure *might* be transient, and
        # `max_attempts` bounds the cost of being wrong about that. The event
        # ends up dead-lettered with the exception's class name on it, which is a
        # far better outcome than an infinite hot loop.
        return await _record_failure(
            factory,
            event_id=event_id,
            exc=exc,
            settings=settings,
            clock=clock,
            started_at=started_at,
        )


async def _apply(
    session: AsyncSession,
    *,
    event: StoredEvent,
    registry: HandlerRegistry,
    settings: Settings,
    clock: Clock,
    started_at: datetime,
) -> AttemptOutcome:
    """The happy path, holding the row lock and the entity lock."""
    structlog.contextvars.bind_contextvars(event_id=event.id, event_type=event.event_type)
    attempt_number = event.attempt_count + 1

    # FR-9. Taken *after* the row is claimed and *before* anything is read, so no
    # two workers can be between the read and the write on one entity at once.
    await lock_entity(
        session,
        entity_type=event.entity_type,
        entity_id=event.entity_id,
        timeout_seconds=settings.advisory_lock_timeout_seconds,
    )

    effect = registry.dispatch(event)
    result = await apply_effect(session, event=event, effect=effect)
    outcome = _SUCCESS[result]

    finished_at = clock.now()
    await queue.record_attempt(
        session,
        event_id=event.id,
        attempt_number=attempt_number,
        started_at=started_at,
        finished_at=finished_at,
        outcome=outcome,
    )
    await queue.mark_succeeded(
        session, event_id=event.id, attempt_number=attempt_number, now=finished_at
    )

    log.info(
        "process.done",
        outcome=outcome.value,
        effect=result.value,
        attempt=attempt_number,
        entity_id=event.entity_id,
    )
    return outcome


async def _record_failure(
    factory: async_sessionmaker[AsyncSession],
    *,
    event_id: int,
    exc: Exception,
    settings: Settings,
    clock: Clock,
    started_at: datetime,
) -> AttemptOutcome | None:
    """Write the failed attempt and decide the event's fate, in a fresh transaction.

    Fresh because the transaction that failed is, if the failure came from the
    database, already aborted -- every statement in it would raise
    `InFailedSqlTransaction`, including the ones trying to record what went wrong.
    """
    retryable = not isinstance(exc, NonRetryableError)
    outcome = AttemptOutcome.RETRYABLE_ERROR if retryable else AttemptOutcome.NON_RETRYABLE_ERROR
    detail = _redacted_detail(exc)

    async with session_scope(factory) as session:
        event = await queue.load_for_update(session, event_id=event_id)
        if event is None:
            # The row is gone. Nothing to record it against, and nothing to retry.
            log.error("process.event_vanished", event_id=event_id, error_class=type(exc).__name__)
            return None

        attempt_number = event.attempt_count + 1
        finished_at = clock.now()
        await queue.record_attempt(
            session,
            event_id=event_id,
            attempt_number=attempt_number,
            started_at=started_at,
            finished_at=finished_at,
            outcome=outcome,
            error_class=type(exc).__name__,
            error_detail=detail,
        )

        exhausted = attempt_number >= settings.max_attempts
        if retryable and not exhausted:
            # TODO(day-3): FR-12 replaces this fixed delay with exponential
            # backoff plus full jitter, from a seedable RNG. The retry *mechanism*
            # is what this slice owes; the schedule is the next slice's.
            next_attempt_at = finished_at + timedelta(seconds=settings.backoff_base_seconds)
            await queue.reschedule(
                session,
                event_id=event_id,
                attempt_number=attempt_number,
                next_attempt_at=next_attempt_at,
                last_error=detail,
            )
            log.warning(
                "process.retry_scheduled",
                event_id=event_id,
                attempt=attempt_number,
                error_class=type(exc).__name__,
            )
            return outcome

        reason = "attempts exhausted" if retryable else "non-retryable failure"
        await queue.dead_letter(
            session,
            event_id=event_id,
            attempts_made=attempt_number,
            reason=f"{reason}: {detail}",
        )
        log.error(
            "process.dead_lettered",
            event_id=event_id,
            attempts=attempt_number,
            reason=reason,
            error_class=type(exc).__name__,
        )
        return outcome


def _redacted_detail(exc: Exception) -> str:
    """A description safe to persist and to log (NFR-6).

    Our own `ProcessingError` messages are written to name fields, never values,
    so they pass through. Anything else -- a driver error, a bug -- gets only its
    class name and a bounded prefix of its message. The engine already suppresses
    bound parameters from SQLAlchemy's exception text, so a payload cannot reach
    a log line through here even by accident.
    """
    message = str(exc) if isinstance(exc, ProcessingError) else f"{type(exc).__name__}: {exc}"
    return message[:ERROR_DETAIL_LIMIT]
