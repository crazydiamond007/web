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

from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from webhook_receiver.adapters import queue
from webhook_receiver.adapters.clock import Clock
from webhook_receiver.adapters.database import session_scope
from webhook_receiver.adapters.failures import is_retryable
from webhook_receiver.adapters.ledger import apply_effect
from webhook_receiver.adapters.locks import lock_entity
from webhook_receiver.adapters.rng import Rng
from webhook_receiver.config import Settings
from webhook_receiver.domain.backoff import next_delay_seconds
from webhook_receiver.domain.effects import EffectResult
from webhook_receiver.domain.enums import AttemptOutcome
from webhook_receiver.domain.errors import ProcessingError
from webhook_receiver.domain.events import StoredEvent
from webhook_receiver.domain.handlers import HandlerRegistry
from webhook_receiver.obs import metrics

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


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """What one pass over one event did.

    `outcome` is what the *attempt* was; `effect` is what happened to the
    business state. They are separate because replay (FR-16) has to tell the
    difference between "I applied the effect" and "the effect was already there
    and I correctly did nothing" -- and both of those are a `SUCCEEDED` attempt.
    Collapsing them would make a replay report success for work it did not do.
    """

    outcome: AttemptOutcome | None
    effect: EffectResult | None

    @property
    def claimed(self) -> bool:
        """Did we get the event at all?

        `False` when another worker held the row (`SKIP LOCKED` stepped over it)
        or it was not due. Normal with several workers on one queue, not an error.
        """
        return self.outcome is not None


NOT_CLAIMED = ProcessResult(outcome=None, effect=None)


async def process_event(
    factory: async_sessionmaker[AsyncSession],
    *,
    event_id: int,
    registry: HandlerRegistry,
    settings: Settings,
    clock: Clock,
    rng: Rng,
) -> ProcessResult:
    """Run one event to a terminal state, or hand it back."""
    started_at = clock.now()
    try:
        async with session_scope(factory) as session:
            event = await queue.claim(session, event_id=event_id, now=started_at)
            if event is None:
                return NOT_CLAIMED
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
        # It does not decide anything: `is_retryable` does, and its default is
        # "no". An exception we cannot classify is far more likely to be our bug
        # than a transient fault, so it is dead-lettered for a human rather than
        # retried five times (SPEC §6.6, adapters/failures.py).
        return await _record_failure(
            factory,
            event_id=event_id,
            exc=exc,
            registry=registry,
            settings=settings,
            clock=clock,
            rng=rng,
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
) -> ProcessResult:
    """The happy path, holding the row lock and the entity lock."""
    structlog.contextvars.bind_contextvars(event_id=event.id, event_type=event.event_type)
    # Two different numbers, and conflating them is what broke replay:
    #   attempt_number -- a fact about history; must never repeat (unique constraint)
    #   attempts_used  -- the retry budget for THIS cycle; replay resets it to 0
    attempt_number = await queue.next_attempt_number(session, event_id=event.id)
    attempts_used = event.attempt_count + 1

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
        session, event_id=event.id, attempts_used=attempts_used, now=finished_at
    )

    label = metrics.known_event_type(event.event_type, registry.event_types)
    metrics.events_processed.labels(event_type=label, outcome=outcome.value).inc()
    metrics.process_latency.labels(event_type=label).observe(
        (finished_at - started_at).total_seconds()
    )

    log.info(
        "process.done",
        outcome=outcome.value,
        effect=result.value,
        attempt=attempt_number,
        entity_id=event.entity_id,
    )
    return ProcessResult(outcome=outcome, effect=result)


async def _record_failure(
    factory: async_sessionmaker[AsyncSession],
    *,
    event_id: int,
    exc: Exception,
    registry: HandlerRegistry,
    settings: Settings,
    clock: Clock,
    rng: Rng,
    started_at: datetime,
) -> ProcessResult:
    """Write the failed attempt and decide the event's fate, in a fresh transaction.

    Fresh because the transaction that failed is, if the failure came from the
    database, already aborted -- every statement in it would raise
    `InFailedSqlTransaction`, including the ones trying to record what went wrong.
    """
    # FR-11. Retryability is *earned*, not assumed: an exception we cannot
    # classify is far more likely to be our own bug than the world's weather, and
    # a bug is not fixed by a fourth attempt (SPEC §6.6). See adapters/failures.py
    # for why the genuinely transient cases have to be enumerated for that default
    # to be safe.
    retryable = is_retryable(exc)
    outcome = AttemptOutcome.RETRYABLE_ERROR if retryable else AttemptOutcome.NON_RETRYABLE_ERROR
    detail = _redacted_detail(exc)

    async with session_scope(factory) as session:
        event = await queue.load_for_update(session, event_id=event_id)
        if event is None:
            # The row is gone. Nothing to record it against, and nothing to retry.
            log.error("process.event_vanished", event_id=event_id, error_class=type(exc).__name__)
            return NOT_CLAIMED

        attempt_number = await queue.next_attempt_number(session, event_id=event_id)
        attempts_used = event.attempt_count + 1
        finished_at = clock.now()
        label = metrics.known_event_type(event.event_type, registry.event_types)
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

        exhausted = attempts_used >= settings.max_attempts
        if retryable and not exhausted:
            # FR-12: exponential, capped, with FULL jitter -- so that a batch of
            # events that failed together does not retry together and knock the
            # recovering downstream straight back over (domain/backoff.py).
            delay = next_delay_seconds(
                attempt=attempt_number,
                base_seconds=settings.backoff_base_seconds,
                cap_seconds=settings.backoff_cap_seconds,
                rng=rng,
            )
            next_attempt_at = finished_at + timedelta(seconds=delay)
            await queue.reschedule(
                session,
                event_id=event_id,
                attempts_used=attempts_used,
                next_attempt_at=next_attempt_at,
                last_error=detail,
            )
            metrics.events_retried.labels(event_type=label, error_class=type(exc).__name__).inc()
            log.warning(
                "process.retry_scheduled",
                event_id=event_id,
                attempt=attempts_used,
                of_max=settings.max_attempts,
                delay_seconds=round(delay, 3),
                error_class=type(exc).__name__,
            )
            return ProcessResult(outcome=outcome, effect=None)

        reason = "attempts exhausted" if retryable else "non-retryable failure"
        # The *metric* label is the coarse reason (two values, bounded). The DLQ
        # row gets the exception class as well, because grouping the queue by
        # failure type is the first thing an operator does with it, and a reason
        # of "non-retryable failure" alone tells them nothing about which one.
        dlq_reason = f"{reason} ({type(exc).__name__}): {detail}"
        metrics.events_dead_lettered.labels(event_type=label, reason=reason).inc()
        await queue.dead_letter(
            session,
            event_id=event_id,
            attempts_made=attempts_used,
            reason=dlq_reason,
        )
        log.error(
            "process.dead_lettered",
            event_id=event_id,
            attempts=attempts_used,
            reason=reason,
            error_class=type(exc).__name__,
        )
        return ProcessResult(outcome=outcome, effect=None)


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
