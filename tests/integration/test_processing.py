"""The processing slice against a real Postgres (FR-6..FR-10, NFR-1).

Every guarantee in this file is a *database* guarantee, so every test here talks
to a real Postgres 16. `SKIP LOCKED`, `pg_advisory_xact_lock` and `ON CONFLICT`
have no meaningful mock: a fake would assert that our idea of Postgres is
self-consistent, which is not the thing in doubt.

The four SPEC §7 Day 2 gates, and the test that proves each:

* concurrent events on one entity are serialised, one ledger row each, correct
  balance -- `test_concurrent_events_on_one_account_land_on_the_correct_balance`
* different entities run in parallel -- `test_a_busy_account_does_not_block_a_different_account`
* out-of-order delivery leaves the newer state -- `test_a_stale_snapshot_is_superseded_not_applied`
* two workers, each event processed once -- `test_two_workers_process_each_event_exactly_once`
"""

from __future__ import annotations

import asyncio
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
from webhook_receiver.adapters.locks import lock_entity
from webhook_receiver.adapters.orm import Account, LedgerEntry, ProcessingAttempt, WebhookEvent
from webhook_receiver.config import Settings
from webhook_receiver.domain.balance import CREDITED, DEBITED, SNAPSHOT, registry
from webhook_receiver.domain.enums import AttemptOutcome, WebhookStatus
from webhook_receiver.domain.events import JsonObject
from webhook_receiver.services.process import process_event
from webhook_receiver.worker.main import poll_once

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)
ACCOUNT = "acct_1"


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        admin_api_key="test-admin-key",
        webhook_secrets={"stripe": "whsec_integration"},
        max_attempts=3,
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


async def seed_event(
    session: AsyncSession,
    *,
    external_id: str,
    event_type: str = CREDITED,
    payload: JsonObject | None = None,
    entity_id: str = ACCOUNT,
    entity_type: str = "account",
    provider_sequence: int | None = None,
) -> int:
    """Insert a pending event, exactly as the ingestion endpoint would have."""
    result = await session.execute(
        sa.insert(WebhookEvent)
        .values(
            source="stripe",
            external_id=external_id,
            idempotency_key=external_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            payload={"amount": 500} if payload is None else payload,
            headers={},
            signature_verified=True,
            occurred_at=NOW,
            provider_sequence=provider_sequence,
            next_attempt_at=NOW,
        )
        .returning(WebhookEvent.id)
    )
    return int(result.scalar_one())


async def balance_of(engine: AsyncEngine, external_ref: str = ACCOUNT) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.select(Account.balance_minor).where(Account.external_ref == external_ref)
        )
        return int(result.scalar_one())


async def count_of(engine: AsyncEngine, table: type[LedgerEntry] | type[ProcessingAttempt]) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(sa.select(sa.func.count()).select_from(table))
        return int(result.scalar_one())


async def event_row(engine: AsyncEngine, event_id: int) -> sa.Row[tuple[WebhookStatus, int]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.select(WebhookEvent.status, WebhookEvent.attempt_count).where(
                WebhookEvent.id == event_id
            )
        )
        return result.one()


class TestExactlyOnce:
    """FR-6: an event produces at most one effect, however many times it runs."""

    async def test_a_credit_is_applied_once(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        async with session_scope(factory) as session:
            event_id = await seed_event(session, external_id="evt_1", payload={"amount": 500})

        outcome = await process_event(
            factory, event_id=event_id, registry=registry, settings=settings, clock=clock
        )

        assert outcome is AttemptOutcome.SUCCEEDED
        assert await balance_of(engine) == 500
        assert await count_of(engine, LedgerEntry) == 1

        status, attempts = await event_row(engine, event_id)
        assert status is WebhookStatus.SUCCEEDED
        assert attempts == 1

    async def test_reprocessing_the_same_event_moves_no_money(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # This is the guarantee the whole service exists for. The second pass is
        # forced by putting the event back to `pending` -- exactly what the Day 3
        # replay endpoint will do (FR-17), and what a crash between commit and
        # acknowledgement would do in production.
        async with session_scope(factory) as session:
            event_id = await seed_event(session, external_id="evt_1", payload={"amount": 500})

        await process_event(
            factory, event_id=event_id, registry=registry, settings=settings, clock=clock
        )
        async with session_scope(factory) as session:
            await session.execute(
                sa.update(WebhookEvent)
                .where(WebhookEvent.id == event_id)
                .values(status=WebhookStatus.PENDING)
            )

        outcome = await process_event(
            factory, event_id=event_id, registry=registry, settings=settings, clock=clock
        )

        # Reported as a success, because it *is* one: the effect exists, which is
        # all the caller ever asked for. The unique constraint stopped the second
        # ledger row, so the balance did not move.
        assert outcome is AttemptOutcome.SUCCEEDED
        assert await balance_of(engine) == 500
        assert await count_of(engine, LedgerEntry) == 1

    async def test_a_debit_is_a_negative_ledger_row(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        async with session_scope(factory) as session:
            credit = await seed_event(session, external_id="evt_1", payload={"amount": 500})
            debit = await seed_event(
                session, external_id="evt_2", event_type=DEBITED, payload={"amount": 200}
            )

        for event_id in (credit, debit):
            await process_event(
                factory, event_id=event_id, registry=registry, settings=settings, clock=clock
            )

        assert await balance_of(engine) == 300
        assert await count_of(engine, LedgerEntry) == 2


class TestClaiming:
    """FR-7: `SKIP LOCKED` means two workers never take the same row."""

    async def test_two_workers_process_each_event_exactly_once(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        events = 12
        async with session_scope(factory) as session:
            for i in range(events):
                await seed_event(
                    session,
                    external_id=f"evt_{i}",
                    payload={"amount": 100},
                    # Spread across accounts, so this measures *row* claiming and
                    # is not accidentally serialised by the entity lock.
                    entity_id=f"acct_{i}",
                )

        # Two independent poll loops racing over the same queue, which is exactly
        # what `docker compose up --scale worker=4` produces.
        await asyncio.gather(
            poll_once(factory, settings=settings, clock=clock, handlers=registry),
            poll_once(factory, settings=settings, clock=clock, handlers=registry),
        )

        # Nothing double-claimed: one ledger row and one attempt per event. A
        # second claim of any row would show up here as an extra ledger row (if
        # the constraint failed) or an extra attempt (if it held).
        assert await count_of(engine, LedgerEntry) == events
        assert await count_of(engine, ProcessingAttempt) == events

        async with engine.connect() as conn:
            pending = await conn.execute(
                sa.select(sa.func.count())
                .select_from(WebhookEvent)
                .where(WebhookEvent.status != WebhookStatus.SUCCEEDED)
            )
            assert pending.scalar_one() == 0  # nothing lost, either


class TestPerEntitySerialisation:
    """FR-9: the advisory lock stops two workers meeting inside one entity."""

    async def test_concurrent_events_on_one_account_land_on_the_correct_balance(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # The load-bearing test of the slice. Ten credits of 100 on ONE account,
        # processed concurrently. Without the advisory lock the workers interleave
        # their read-modify-write and the balance comes out short -- and `SKIP
        # LOCKED` alone would not save it, because these are ten *different* rows.
        events = 10
        async with session_scope(factory) as session:
            ids = [
                await seed_event(session, external_id=f"evt_{i}", payload={"amount": 100})
                for i in range(events)
            ]

        await asyncio.gather(
            *(
                process_event(
                    factory, event_id=event_id, registry=registry, settings=settings, clock=clock
                )
                for event_id in ids
            )
        )

        assert await balance_of(engine) == events * 100
        assert await count_of(engine, LedgerEntry) == events

        # NFR-1's headline invariant, stated as a query rather than as a hope.
        async with engine.connect() as conn:
            total = await conn.execute(sa.select(sa.func.sum(LedgerEntry.amount_minor)))
            assert total.scalar_one() == await balance_of(engine)

    async def test_a_busy_account_does_not_block_a_different_account(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # "Different entities still run in parallel" (FR-9), asserted without a
        # sleep or a timing race: hold acct_1's lock open in one transaction, and
        # show that an event for acct_2 sails through anyway. If the lock were
        # global -- or keyed on something too coarse -- this would time out.
        async with session_scope(factory) as session:
            other = await seed_event(
                session, external_id="evt_other", entity_id="acct_2", payload={"amount": 700}
            )

        async with session_scope(factory) as blocker:
            await lock_entity(blocker, entity_type="account", entity_id=ACCOUNT, timeout_seconds=5)

            outcome = await process_event(
                factory, event_id=other, registry=registry, settings=settings, clock=clock
            )

        assert outcome is AttemptOutcome.SUCCEEDED
        assert await balance_of(engine, "acct_2") == 700

    async def test_an_event_for_a_locked_account_is_retried_not_failed(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # Contention is a fact about the world, not about the event: the event is
        # rescheduled, not dead-lettered. `lock_timeout` is dropped to 100ms so
        # the test does not wait out the 5s default.
        settings = settings.model_copy(update={"advisory_lock_timeout_seconds": 0.1})
        async with session_scope(factory) as session:
            event_id = await seed_event(session, external_id="evt_1", payload={"amount": 500})

        async with session_scope(factory) as blocker:
            await lock_entity(blocker, entity_type="account", entity_id=ACCOUNT, timeout_seconds=5)

            outcome = await process_event(
                factory, event_id=event_id, registry=registry, settings=settings, clock=clock
            )

        assert outcome is AttemptOutcome.RETRYABLE_ERROR
        assert await count_of(engine, LedgerEntry) == 0

        status, attempts = await event_row(engine, event_id)
        assert status is WebhookStatus.PENDING  # back in the queue, not dead
        assert attempts == 1

        async with engine.connect() as conn:
            row = await conn.execute(
                sa.select(WebhookEvent.next_attempt_at, WebhookEvent.last_error).where(
                    WebhookEvent.id == event_id
                )
            )
            next_attempt_at, last_error = row.one()

        assert next_attempt_at > NOW  # not due again immediately
        assert "LockContentionError" in last_error or "holding it" in last_error


class TestOrdering:
    """FR-10: a late event must not clobber newer state."""

    async def test_a_stale_snapshot_is_superseded_not_applied(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # The newer snapshot arrives first (sequence 2, balance 1000); the older
        # one turns up late (sequence 1, balance 50). Applying it would silently
        # rewind the account by 950 -- the exact failure FR-10 exists to prevent.
        async with session_scope(factory) as session:
            newer = await seed_event(
                session,
                external_id="evt_new",
                event_type=SNAPSHOT,
                payload={"balance": 1000},
                provider_sequence=2,
            )
            older = await seed_event(
                session,
                external_id="evt_old",
                event_type=SNAPSHOT,
                payload={"balance": 50},
                provider_sequence=1,
            )

        assert (
            await process_event(
                factory, event_id=newer, registry=registry, settings=settings, clock=clock
            )
            is AttemptOutcome.SUCCEEDED
        )
        stale_outcome = await process_event(
            factory, event_id=older, registry=registry, settings=settings, clock=clock
        )

        assert stale_outcome is AttemptOutcome.SUPERSEDED
        assert await balance_of(engine) == 1000  # the newer state survived
        assert await count_of(engine, LedgerEntry) == 1  # the stale event applied nothing

        # Terminal, not retried: arriving late is not a failure, and re-running it
        # would only reach the same conclusion. But the attempt log says plainly
        # what happened, so out-of-order delivery is a queryable rate rather than
        # an inference (ADR-0006).
        status, _ = await event_row(engine, older)
        assert status is WebhookStatus.SUCCEEDED

        async with engine.connect() as conn:
            outcomes = await conn.execute(
                sa.select(ProcessingAttempt.outcome).order_by(ProcessingAttempt.id)
            )
            assert list(outcomes.scalars()) == [
                AttemptOutcome.SUCCEEDED,
                AttemptOutcome.SUPERSEDED,
            ]

    async def test_a_credit_is_never_superseded(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # Addition is commutative, so a late credit is not stale -- it is late.
        # Discarding it would lose money. It applies, and it does not rewind the
        # high-water mark either.
        async with session_scope(factory) as session:
            snapshot = await seed_event(
                session,
                external_id="evt_snap",
                event_type=SNAPSHOT,
                payload={"balance": 1000},
                provider_sequence=5,
            )
            late_credit = await seed_event(
                session, external_id="evt_late", payload={"amount": 250}, provider_sequence=2
            )

        for event_id in (snapshot, late_credit):
            await process_event(
                factory, event_id=event_id, registry=registry, settings=settings, clock=clock
            )

        assert await balance_of(engine) == 1250
        assert await count_of(engine, LedgerEntry) == 2

        async with engine.connect() as conn:
            version = await conn.execute(
                sa.select(Account.version).where(Account.external_ref == ACCOUNT)
            )
            # GREATEST, not assignment: the out-of-order credit must not drag the
            # mark back to 2, or the next stale snapshot would be applied.
            assert version.scalar_one() == 5


class TestFailure:
    """FR-8, FR-11, FR-14: what happens to an event we cannot process."""

    async def test_an_unknown_event_type_is_dead_lettered_not_dropped(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        async with session_scope(factory) as session:
            event_id = await seed_event(session, external_id="evt_1", event_type="invoice.exploded")

        outcome = await process_event(
            factory, event_id=event_id, registry=registry, settings=settings, clock=clock
        )

        # Straight to the DLQ on the first attempt: it will be just as unknown on
        # the fifth, and retrying it would only delay the events behind it.
        assert outcome is AttemptOutcome.NON_RETRYABLE_ERROR

        status, attempts = await event_row(engine, event_id)
        assert status is WebhookStatus.DEAD_LETTERED
        assert attempts == 1
        assert await count_of(engine, LedgerEntry) == 0

        async with engine.connect() as conn:
            dlq = await conn.execute(
                sa.text("SELECT event_id, reason, attempts_made, status FROM dead_letter_entry")
            )
            entry = dlq.one()

        assert entry.event_id == event_id
        assert entry.attempts_made == 1
        assert entry.status == "needs_review"
        assert "UnknownEventTypeError" in entry.reason or "no handler" in entry.reason

    async def test_a_malformed_payload_never_reaches_the_error_column(
        self,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        clock: FixedClock,
    ) -> None:
        # NFR-6. The amount is a string here, so the handler refuses it -- and the
        # message it writes to `last_error` must name the field, never the value.
        secret = "4111111111111111"
        async with session_scope(factory) as session:
            event_id = await seed_event(session, external_id="evt_1", payload={"amount": secret})

        await process_event(
            factory, event_id=event_id, registry=registry, settings=settings, clock=clock
        )

        async with engine.connect() as conn:
            rows = await conn.execute(
                sa.select(WebhookEvent.last_error, ProcessingAttempt.error_detail).join(
                    ProcessingAttempt, ProcessingAttempt.event_id == WebhookEvent.id
                )
            )
            last_error, error_detail = rows.one()

        assert secret not in (last_error or "")
        assert secret not in (error_detail or "")
        assert "amount" in (error_detail or "")
