"""Re-processing an event on purpose (FR-16, FR-17).

Replay does **not** get its own path. It requeues the event and sends it through
the exact same `process_event` the worker uses -- the same claim, the same
advisory lock, the same unique-keyed ledger. That is the entire safety argument
for FR-17: replaying an already-processed event adds no effect, not because replay
remembers to check, but because the `uq_ledger_entry_event_id` constraint it runs
into is the same one that makes ordinary redelivery safe. A separate "replay path"
would be a second implementation of exactly-once, and the second implementation is
the one that has the bug.

So the load test can hurl 10,000 already-processed events at this and land on zero
new ledger rows, without this module containing a single line about ledgers.

What replay adds on top is the audit (FR-16): who asked, when, why, and how it
went. That is a `replay_request` row per event, always written -- including when
the replay failed, which is the case an operator most needs to be able to find
later.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from webhook_receiver.adapters import dlq, queue
from webhook_receiver.adapters.clock import Clock
from webhook_receiver.adapters.database import session_scope
from webhook_receiver.adapters.rng import Rng
from webhook_receiver.config import Settings
from webhook_receiver.domain.effects import EffectResult
from webhook_receiver.domain.enums import DlqStatus, ReplayOutcome
from webhook_receiver.domain.handlers import HandlerRegistry
from webhook_receiver.obs import metrics
from webhook_receiver.services.process import ProcessResult, process_event

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReplayResult:
    event_id: int
    outcome: ReplayOutcome
    detail: str | None = None


def _outcome_of(result: ProcessResult) -> tuple[ReplayOutcome, str | None]:
    """Translate what processing did into what the replay achieved."""
    if not result.claimed:
        # A worker grabbed the event between our requeue and our claim. The event
        # is in hand and its effect will still happen exactly once -- but *this
        # replay* did nothing, and saying otherwise would credit it with work it
        # did not do.
        return ReplayOutcome.SKIPPED_ALREADY_PROCESSED, "claimed by a worker first"

    match result.effect:
        case EffectResult.APPLIED:
            return ReplayOutcome.SUCCEEDED, None
        case EffectResult.ALREADY_APPLIED:
            # FR-17, and the whole point. The ledger's unique constraint refused a
            # second effect, so the replay was a no-op -- which is a *success* of
            # the guarantee, not a failure of the replay.
            return ReplayOutcome.SKIPPED_ALREADY_PROCESSED, "effect already existed"
        case EffectResult.SUPERSEDED:
            # Newer state won; the event was correctly not applied (FR-10). Nothing
            # was skipped *because it was already processed*, strictly speaking --
            # but nothing happened, and `succeeded` would imply an effect exists.
            # Of the three outcomes the spec allows, this is the only honest one.
            return ReplayOutcome.SKIPPED_ALREADY_PROCESSED, "superseded by newer state"
        case None:
            return ReplayOutcome.FAILED, "processing failed; see the attempt history"


async def replay_event(
    factory: async_sessionmaker[AsyncSession],
    *,
    event_id: int,
    requested_by: str,
    reason: str | None,
    registry: HandlerRegistry,
    settings: Settings,
    clock: Clock,
    rng: Rng,
) -> ReplayResult:
    """Requeue one event, process it, and record that we did."""
    now = clock.now()

    async with session_scope(factory) as session:
        event = await queue.load_for_update(session, event_id=event_id)
        if event is None:
            msg = f"event {event_id} does not exist"
            raise EventNotFoundError(msg)

        await queue.requeue(session, event_id=event_id, now=now)
        # If it is sitting in the DLQ, say so before we start: an operator watching
        # the queue should see `replaying`, not a row that appears untouched while
        # something is quietly working on it.
        await _mark_dlq_replaying(session, event_id=event_id, now=now)

    result = await process_event(
        factory,
        event_id=event_id,
        registry=registry,
        settings=settings,
        clock=clock,
        rng=rng,
    )
    outcome, detail = _outcome_of(result)

    async with session_scope(factory) as session:
        await queue.record_replay_request(
            session,
            event_id=event_id,
            requested_by=requested_by,
            reason=reason,
            outcome=outcome,
            requested_at=now,
            resulting_attempt_id=await queue.latest_attempt_id(session, event_id=event_id),
        )
        await _settle_dlq(session, event_id=event_id, outcome=outcome, now=clock.now())

    metrics.events_replayed.labels(outcome=outcome.value).inc()
    log.info(
        "replay.done",
        event_id=event_id,
        outcome=outcome.value,
        detail=detail,
        requested_by=requested_by,
    )
    return ReplayResult(event_id=event_id, outcome=outcome, detail=detail)


async def replay_events(
    factory: async_sessionmaker[AsyncSession],
    *,
    event_ids: Sequence[int],
    requested_by: str,
    reason: str | None,
    registry: HandlerRegistry,
    settings: Settings,
    clock: Clock,
    rng: Rng,
) -> list[ReplayResult]:
    """Replay a batch, one event at a time.

    Sequential, not `asyncio.gather`. A replay is an operator action against a
    live system, and firing a hundred concurrent transactions at it -- each taking
    an advisory lock -- is how a well-meant replay becomes the outage. The worker
    fleet is where throughput comes from; this is where control does.
    """
    results = []
    for event_id in event_ids:
        results.append(
            await replay_event(
                factory,
                event_id=event_id,
                requested_by=requested_by,
                reason=reason,
                registry=registry,
                settings=settings,
                clock=clock,
                rng=rng,
            )
        )
    return results


class EventNotFoundError(LookupError):
    """The event asked for does not exist. Surfaces as a 404, not a 500."""


async def _mark_dlq_replaying(session: AsyncSession, *, event_id: int, now: datetime) -> None:
    entry = await dlq.entry_for_event(session, event_id=event_id)
    if entry is None or entry.status is not DlqStatus.NEEDS_REVIEW:
        # Not dead-lettered, or already terminal. Replaying a `resolved` entry is
        # allowed -- the *event* can always be replayed -- but it does not drag the
        # entry back out of a state a human deliberately put it in.
        return
    await dlq.transition(
        session, entry_id=entry.id, target=DlqStatus.REPLAYING, now=now, note="replay requested"
    )


async def _settle_dlq(
    session: AsyncSession, *, event_id: int, outcome: ReplayOutcome, now: datetime
) -> None:
    entry = await dlq.entry_for_event(session, event_id=event_id)
    if entry is None or entry.status is not DlqStatus.REPLAYING:
        return

    if outcome is ReplayOutcome.FAILED:
        # Back to the humans. Leaving it in `replaying` would strand it in a state
        # nothing is watching.
        await dlq.transition(
            session,
            entry_id=entry.id,
            target=DlqStatus.NEEDS_REVIEW,
            now=now,
            note="replay failed",
        )
        return

    await dlq.transition(
        session,
        entry_id=entry.id,
        target=DlqStatus.RESOLVED,
        now=now,
        note=f"replay {outcome.value}",
    )
