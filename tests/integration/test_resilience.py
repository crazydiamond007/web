"""Retry, dead-letter, and replay against a real Postgres (FR-11..FR-17).

The SPEC §7 Day 3 gates, and the test that proves each:

* forced retryable failure -> the backoff schedule
      `TestRetry::test_successive_failures_back_off_and_do_not_line_up`
* forced non-retryable   -> straight to the DLQ, no retries burned
      `TestRetry::test_a_non_retryable_failure_does_not_burn_retries`
* exhausted retries      -> the DLQ
      `TestRetry::test_retries_are_bounded_and_the_move_to_the_dlq_is_recorded`
* replay of a processed event -> no new effect
      `TestReplay::test_replaying_a_processed_event_adds_no_effect`
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from webhook_receiver.adapters.clock import FixedClock
from webhook_receiver.adapters.database import create_session_factory, session_scope
from webhook_receiver.adapters.orm import (
    DeadLetterEntry,
    LedgerEntry,
    ProcessingAttempt,
    ReplayRequest,
    WebhookEvent,
)
from webhook_receiver.adapters.rng import SeededRng
from webhook_receiver.config import Settings
from webhook_receiver.domain.balance import CREDITED, registry
from webhook_receiver.domain.effects import Credit
from webhook_receiver.domain.enums import AttemptOutcome, DlqStatus, ReplayOutcome, WebhookStatus
from webhook_receiver.domain.errors import RetryableError
from webhook_receiver.domain.events import StoredEvent
from webhook_receiver.domain.handlers import HandlerRegistry
from webhook_receiver.services.process import process_event
from webhook_receiver.services.replay import replay_event

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)
ACCOUNT = "acct_1"
MAX_ATTEMPTS = 3


class DownstreamError(RetryableError):
    """A transient failure we can turn on and off from a test."""


def failing_registry(*, exc: Exception) -> HandlerRegistry:
    """A registry whose handler always fails, so the retry path can be driven."""
    local = HandlerRegistry()

    def explode(event: StoredEvent) -> Credit:
        raise exc

    local.register(CREDITED)(explode)
    return local


def flaky_registry(*, fail_times: int) -> HandlerRegistry:
    """Fails `fail_times`, then succeeds -- a downstream that comes back."""
    local = HandlerRegistry()
    calls = {"n": 0}

    def handle(event: StoredEvent) -> Credit:
        calls["n"] += 1
        if calls["n"] <= fail_times:
            msg = "downstream 503"
            raise DownstreamError(msg)
        return Credit(account_ref=event.entity_id, amount_minor=500)

    local.register(CREDITED)(handle)
    return local


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        admin_api_key="test-admin-key",
        webhook_secrets={"stripe": "whsec_integration"},
        max_attempts=MAX_ATTEMPTS,
        backoff_base_seconds=1.0,
        backoff_cap_seconds=300.0,
        _env_file=None,
    )


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
async def engine(settings: Settings, migrated_schema: None) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(str(settings.database_url))
    yield eng
    await eng.dispose()


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def seed_event(session: AsyncSession, *, external_id: str = "evt_1") -> int:
    result = await session.execute(
        sa.insert(WebhookEvent)
        .values(
            source="stripe",
            external_id=external_id,
            idempotency_key=external_id,
            event_type=CREDITED,
            entity_type="account",
            entity_id=ACCOUNT,
            payload={"amount": 500},
            headers={},
            signature_verified=True,
            occurred_at=NOW,
            next_attempt_at=NOW,
        )
        .returning(WebhookEvent.id)
    )
    return int(result.scalar_one())


async def event_row(
    engine: AsyncEngine, event_id: int
) -> sa.Row[tuple[WebhookStatus, int, datetime, str | None]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.select(
                WebhookEvent.status,
                WebhookEvent.attempt_count,
                WebhookEvent.next_attempt_at,
                WebhookEvent.last_error,
            ).where(WebhookEvent.id == event_id)
        )
        return result.one()


async def count_of(engine: AsyncEngine, table: type[object]) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(sa.select(sa.func.count()).select_from(table))
        return int(result.scalar_one())


class TestRetry:
    """FR-11, FR-12, FR-13."""

    async def test_successive_failures_back_off_and_do_not_line_up(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # FR-12's acceptance, both halves. Ten events fail on the same attempt at
        # the same instant, and their next_attempt_at values must (a) sit inside
        # the schedule's ceiling and (b) not be identical -- or the whole batch
        # would come back at once and knock the downstream over again.
        handlers = failing_registry(exc=DownstreamError("downstream 503"))
        settings = settings.model_copy(update={"max_attempts": 5})

        async with session_scope(factory) as session:
            ids = [await seed_event(session, external_id=f"evt_{i}") for i in range(10)]

        for event_id in ids:
            result = await process_event(
                factory,
                event_id=event_id,
                registry=handlers,
                settings=settings,
                clock=clock,
                rng=SeededRng(event_id),  # a different draw per event, as in a real fleet
            )
            assert result.outcome is AttemptOutcome.RETRYABLE_ERROR

        delays = []
        for event_id in ids:
            status, attempts, next_at, _ = await event_row(engine, event_id)
            assert status is WebhookStatus.PENDING  # back in the queue, not dead
            assert attempts == 1
            delays.append((next_at - NOW).total_seconds())

        # The ceiling after attempt 1 is min(cap, base * 2**1) = 2s.
        assert all(0.0 <= d <= 2.0 for d in delays)
        # And the point of jitter: they are spread, not stacked.
        assert len(set(delays)) == len(delays)

    async def test_the_ceiling_grows_with_each_attempt(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # Drive one event through three failures and watch the *envelope* widen:
        # 2s, then 4s, then 8s. The draw inside it is random, so the assertion is
        # on the bound, not on the sample.
        handlers = failing_registry(exc=DownstreamError("downstream 503"))
        settings = settings.model_copy(update={"max_attempts": 10})

        async with session_scope(factory) as session:
            event_id = await seed_event(session)

        ceilings = []
        for _ in range(3):
            await process_event(
                factory,
                event_id=event_id,
                registry=handlers,
                settings=settings,
                clock=clock,
                rng=SeededRng(1),
            )
            _, attempts, next_at, _ = await event_row(engine, event_id)
            ceilings.append(((next_at - clock.now()).total_seconds(), attempts))
            # The clock does not move on its own; move it past the deadline so the
            # event is due again. No sleeping: time is an input (SPEC §6.4).
            clock.advance((next_at - clock.now()).total_seconds() + 1)

        (d1, a1), (d2, a2), (d3, a3) = ceilings
        assert (a1, a2, a3) == (1, 2, 3)
        assert 0 <= d1 <= 2.0
        assert 0 <= d2 <= 4.0
        assert 0 <= d3 <= 8.0

    async def test_a_non_retryable_failure_does_not_burn_retries(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # FR-11's acceptance, stated exactly: straight to the DLQ on attempt 1.
        # A TypeError is an unclassified failure -- our bug -- and SPEC §6.6 says
        # that is non-retryable. It will be just as broken on the fifth attempt.
        handlers = failing_registry(exc=TypeError("NoneType is not subscriptable"))

        async with session_scope(factory) as session:
            event_id = await seed_event(session)

        result = await process_event(
            factory,
            event_id=event_id,
            registry=handlers,
            settings=settings,
            clock=clock,
            rng=SeededRng(1),
        )

        assert result.outcome is AttemptOutcome.NON_RETRYABLE_ERROR

        status, attempts, _, _ = await event_row(engine, event_id)
        assert status is WebhookStatus.DEAD_LETTERED
        assert attempts == 1  # one, not MAX_ATTEMPTS: the budget was not burned
        assert await count_of(engine, ProcessingAttempt) == 1
        assert await count_of(engine, DeadLetterEntry) == 1

    async def test_retries_are_bounded_and_the_move_to_the_dlq_is_recorded(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # FR-13. A permanently sick downstream must not be retried forever.
        handlers = failing_registry(exc=DownstreamError("downstream 503"))

        async with session_scope(factory) as session:
            event_id = await seed_event(session)

        for _ in range(MAX_ATTEMPTS):
            await process_event(
                factory,
                event_id=event_id,
                registry=handlers,
                settings=settings,
                clock=clock,
                rng=SeededRng(1),
            )
            clock.advance(3600)  # jump past whatever backoff was scheduled

        status, attempts, _, last_error = await event_row(engine, event_id)
        assert status is WebhookStatus.DEAD_LETTERED
        assert attempts == MAX_ATTEMPTS
        assert await count_of(engine, ProcessingAttempt) == MAX_ATTEMPTS
        assert "attempts exhausted" in last_error

        async with engine.connect() as conn:
            entry = (
                await conn.execute(
                    sa.select(
                        DeadLetterEntry.attempts_made,
                        DeadLetterEntry.status,
                        DeadLetterEntry.reason,
                    )
                )
            ).one()

        assert entry.attempts_made == MAX_ATTEMPTS
        assert entry.status is DlqStatus.NEEDS_REVIEW
        assert "attempts exhausted" in entry.reason

    async def test_a_poison_event_does_not_hold_up_healthy_traffic(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # FR-14's acceptance. The poison event dead-letters itself and the healthy
        # one behind it is applied -- rather than the queue head-of-line blocking
        # on something that will never work.
        async with session_scope(factory) as session:
            poison = await seed_event(session, external_id="evt_poison")
            healthy = await seed_event(session, external_id="evt_healthy")

        await process_event(
            factory,
            event_id=poison,
            registry=failing_registry(exc=TypeError("bug")),
            settings=settings,
            clock=clock,
            rng=SeededRng(1),
        )
        await process_event(
            factory,
            event_id=healthy,
            registry=registry,
            settings=settings,
            clock=clock,
            rng=SeededRng(1),
        )

        poison_status, _, _, _ = await event_row(engine, poison)
        healthy_status, _, _, _ = await event_row(engine, healthy)

        assert poison_status is WebhookStatus.DEAD_LETTERED
        assert healthy_status is WebhookStatus.SUCCEEDED
        assert await count_of(engine, LedgerEntry) == 1  # only the healthy one moved money

    async def test_a_downstream_that_recovers_is_processed_normally(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # The case the whole retry machinery exists for: it failed twice, came
        # back, and the event was applied exactly once.
        handlers = flaky_registry(fail_times=2)

        async with session_scope(factory) as session:
            event_id = await seed_event(session)

        for _ in range(3):
            await process_event(
                factory,
                event_id=event_id,
                registry=handlers,
                settings=settings,
                clock=clock,
                rng=SeededRng(1),
            )
            clock.advance(3600)

        status, _, _, _ = await event_row(engine, event_id)
        assert status is WebhookStatus.SUCCEEDED
        assert await count_of(engine, LedgerEntry) == 1
        assert await count_of(engine, DeadLetterEntry) == 0

        async with engine.connect() as conn:
            outcomes = await conn.execute(
                sa.select(ProcessingAttempt.outcome).order_by(ProcessingAttempt.attempt_number)
            )
            assert list(outcomes.scalars()) == [
                AttemptOutcome.RETRYABLE_ERROR,
                AttemptOutcome.RETRYABLE_ERROR,
                AttemptOutcome.SUCCEEDED,
            ]


class TestReplay:
    """FR-16, FR-17."""

    async def test_replaying_a_processed_event_adds_no_effect(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # FR-17, the headline. The replay goes through the same claim, the same
        # advisory lock, and the same unique-keyed ledger as the worker -- so the
        # constraint that makes redelivery safe makes replay safe, for free.
        async with session_scope(factory) as session:
            event_id = await seed_event(session)

        await process_event(
            factory,
            event_id=event_id,
            registry=registry,
            settings=settings,
            clock=clock,
            rng=SeededRng(1),
        )
        assert await count_of(engine, LedgerEntry) == 1

        result = await replay_event(
            factory,
            event_id=event_id,
            requested_by="ops@example.com",
            reason="checking idempotency",
            registry=registry,
            settings=settings,
            clock=clock,
            rng=SeededRng(1),
        )

        assert result.outcome is ReplayOutcome.SKIPPED_ALREADY_PROCESSED
        assert await count_of(engine, LedgerEntry) == 1  # zero new effects

        async with engine.connect() as conn:
            balance = await conn.execute(sa.text("SELECT balance_minor FROM account"))
            assert balance.scalar_one() == 500  # the money did not move twice

    async def test_replay_is_audited_even_when_it_does_nothing(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # FR-16's acceptance: who, when, why, and how it went. An audit trail that
        # only records the successes is a marketing document.
        async with session_scope(factory) as session:
            event_id = await seed_event(session)

        await process_event(
            factory,
            event_id=event_id,
            registry=registry,
            settings=settings,
            clock=clock,
            rng=SeededRng(1),
        )
        await replay_event(
            factory,
            event_id=event_id,
            requested_by="ops@example.com",
            reason="ticket 4471",
            registry=registry,
            settings=settings,
            clock=clock,
            rng=SeededRng(1),
        )

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(
                        ReplayRequest.event_id,
                        ReplayRequest.requested_by,
                        ReplayRequest.reason,
                        ReplayRequest.outcome,
                        ReplayRequest.resulting_attempt_id,
                    )
                )
            ).one()

        assert row.event_id == event_id
        assert row.requested_by == "ops@example.com"
        assert row.reason == "ticket 4471"
        assert row.outcome is ReplayOutcome.SKIPPED_ALREADY_PROCESSED
        assert row.resulting_attempt_id is not None

    async def test_replaying_a_dead_lettered_event_gives_it_a_fresh_budget(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # The story FR-16 exists for: a poison event lands in the DLQ, somebody
        # fixes the handler, the event is replayed, and it works.
        #
        # It also pins the bug that separating attempt_number from attempt_count
        # fixed: the replay writes attempt 2, not a second attempt 1, so the
        # unique constraint on (event_id, attempt_number) does not reject it.
        async with session_scope(factory) as session:
            event_id = await seed_event(session)

        await process_event(
            factory,
            event_id=event_id,
            registry=failing_registry(exc=TypeError("the bug")),
            settings=settings,
            clock=clock,
            rng=SeededRng(1),
        )
        status, _, _, _ = await event_row(engine, event_id)
        assert status is WebhookStatus.DEAD_LETTERED

        # ... the bug is fixed, and the real handler now works.
        result = await replay_event(
            factory,
            event_id=event_id,
            requested_by="ops@example.com",
            reason="handler fixed in v1.2",
            registry=registry,
            settings=settings,
            clock=clock,
            rng=SeededRng(1),
        )

        assert result.outcome is ReplayOutcome.SUCCEEDED
        assert await count_of(engine, LedgerEntry) == 1

        status, attempts, _, _ = await event_row(engine, event_id)
        assert status is WebhookStatus.SUCCEEDED
        assert attempts == 1  # a fresh budget, not 1 + the attempt it already spent

        async with engine.connect() as conn:
            numbers = await conn.execute(
                sa.select(ProcessingAttempt.attempt_number).order_by(
                    ProcessingAttempt.attempt_number
                )
            )
            # The history is intact and monotonic -- an audit log that forgets is
            # not one.
            assert list(numbers.scalars()) == [1, 2]

    async def test_a_successful_replay_resolves_the_dlq_entry(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        async with session_scope(factory) as session:
            event_id = await seed_event(session)

        await process_event(
            factory,
            event_id=event_id,
            registry=failing_registry(exc=TypeError("the bug")),
            settings=settings,
            clock=clock,
            rng=SeededRng(1),
        )
        await replay_event(
            factory,
            event_id=event_id,
            requested_by="ops",
            reason=None,
            registry=registry,
            settings=settings,
            clock=clock,
            rng=SeededRng(1),
        )

        async with engine.connect() as conn:
            entry = (
                await conn.execute(
                    sa.select(
                        DeadLetterEntry.status,
                        DeadLetterEntry.resolved_at,
                        DeadLetterEntry.resolution_note,
                    )
                )
            ).one()

        assert entry.status is DlqStatus.RESOLVED
        assert entry.resolved_at is not None
        assert "replay" in entry.resolution_note

    async def test_a_failed_replay_returns_the_entry_to_the_humans(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # The bug was NOT fixed. The entry must not be left stranded in
        # `replaying`, where nothing is watching it.
        async with session_scope(factory) as session:
            event_id = await seed_event(session)

        broken = failing_registry(exc=TypeError("still broken"))
        await process_event(
            factory,
            event_id=event_id,
            registry=broken,
            settings=settings,
            clock=clock,
            rng=SeededRng(1),
        )
        result = await replay_event(
            factory,
            event_id=event_id,
            requested_by="ops",
            reason="hoping",
            registry=broken,
            settings=settings,
            clock=clock,
            rng=SeededRng(1),
        )

        assert result.outcome is ReplayOutcome.FAILED

        async with engine.connect() as conn:
            entry = (
                await conn.execute(
                    sa.select(DeadLetterEntry.status, DeadLetterEntry.resolution_note)
                )
            ).one()

        assert entry.status is DlqStatus.NEEDS_REVIEW
        assert entry.resolution_note == "replay failed"
        assert await count_of(engine, DeadLetterEntry) == 1  # still exactly one entry
