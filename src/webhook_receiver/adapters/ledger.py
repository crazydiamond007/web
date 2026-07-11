"""Applying an effect, exactly once, in the right order (FR-6, FR-10).

This is the file the whole service exists to make safe. Two guarantees meet here,
and they are enforced by two different things:

* **Exactly once** is `uq_ledger_entry_event_id`. Not a check, not a flag we set
  and hope to have set -- a `UNIQUE` constraint. `INSERT ... ON CONFLICT DO
  NOTHING RETURNING id` comes back empty when the effect already exists, and it
  does so atomically, under concurrency, even if the two attempts are running on
  different machines. The balance can only move on the *same statement* that
  successfully claims the ledger row, so "effect applied" and "effect recorded"
  are the same event and cannot drift apart.

* **In order** is `account.version`, the sequence of the newest event applied to
  the account. Only ``SetBalance`` consults it, because only ``SetBalance`` can
  be made wrong by arriving late. See ``domain/effects.py``.

Every function here assumes the caller already holds the entity's advisory lock
(FR-9). That is what lets ``apply_set_balance`` read the balance and write it back
without a `WHERE version = :expected` retry loop: nobody else is touching this
account. If that assumption is ever broken, the balance is corruptible -- so the
lock is taken in exactly one place, ``services/process.py``, and never here.
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from webhook_receiver.adapters.orm import Account, LedgerEntry
from webhook_receiver.domain.effects import Credit, Effect, EffectResult, SetBalance
from webhook_receiver.domain.events import StoredEvent

LEDGER_CONSTRAINT = "uq_ledger_entry_event_id"
ACCOUNT_CONSTRAINT = "uq_account_external_ref"


async def apply_effect(
    session: AsyncSession, *, event: StoredEvent, effect: Effect
) -> EffectResult:
    """Apply `effect` on behalf of `event`. Idempotent by construction."""
    match effect:
        case Credit():
            return await _apply_credit(session, event=event, effect=effect)
        case SetBalance():
            return await _apply_set_balance(session, event=event, effect=effect)


async def _account_id(session: AsyncSession, external_ref: str) -> int:
    """Find the account, creating it on first sight of it.

    An upsert rather than a `SELECT` then an `INSERT`: two events for a brand-new
    account, arriving at two workers at the same instant, would both see nothing
    and both insert. The advisory lock happens to prevent that today -- but only
    because the lock is keyed on the entity, and relying on that would make this
    function silently unsafe the moment anyone calls it from a path that does not
    hold the lock. The constraint costs nothing and does not depend on being
    called correctly.
    """
    inserted = (
        await session.execute(
            insert(Account)
            .values(external_ref=external_ref)
            .on_conflict_do_nothing(constraint=ACCOUNT_CONSTRAINT)
            .returning(Account.id)
        )
    ).scalar_one_or_none()

    if inserted is not None:
        return inserted

    return (
        await session.execute(select(Account.id).where(Account.external_ref == external_ref))
    ).scalar_one()


async def _claim_ledger_row(
    session: AsyncSession, *, account_id: int, event_id: int, amount_minor: int
) -> bool:
    """Write the effect, or discover that it is already written (FR-6).

    Returns whether *this* call is the one that applied it. An empty `RETURNING`
    means the unique constraint rejected a second row for this event -- the
    effect exists, the balance already moved, and there is nothing to do.
    """
    ledger_id = (
        await session.execute(
            insert(LedgerEntry)
            .values(account_id=account_id, event_id=event_id, amount_minor=amount_minor)
            .on_conflict_do_nothing(constraint=LEDGER_CONSTRAINT)
            .returning(LedgerEntry.id)
        )
    ).scalar_one_or_none()
    return ledger_id is not None


async def _apply_credit(
    session: AsyncSession, *, event: StoredEvent, effect: Credit
) -> EffectResult:
    """Additive, so it is never superseded -- only ever applied or already applied.

    A late credit is not stale, it is late. Discarding it because a newer event
    got here first would lose money, and money that a provider told us about is
    the one thing we are not allowed to lose.
    """
    account_id = await _account_id(session, effect.account_ref)

    if not await _claim_ledger_row(
        session,
        account_id=account_id,
        event_id=event.id,
        amount_minor=effect.amount_minor,
    ):
        return EffectResult.ALREADY_APPLIED

    await session.execute(
        update(Account)
        .where(Account.id == account_id)
        .values(
            # `balance + :amount`, computed by Postgres from the row it is
            # writing -- never `balance = :value_we_read_earlier`. There is no
            # window between the read and the write for the value to go stale in.
            balance_minor=Account.balance_minor + effect.amount_minor,
            # A credit still advances the high-water mark, so a snapshot that
            # predates it is correctly recognised as stale. GREATEST, not
            # assignment: an out-of-order credit must not *lower* the mark.
            version=func.greatest(Account.version, event.provider_sequence or 0),
            updated_at=func.now(),
        )
    )
    return EffectResult.APPLIED


async def _apply_set_balance(
    session: AsyncSession, *, event: StoredEvent, effect: SetBalance
) -> EffectResult:
    """Last-writer-wins, so it is guarded by the sequence it carries (FR-10).

    The ledger row records the *delta* this snapshot represents, not the absolute
    balance, so `balance == SUM(ledger_entry.amount_minor)` keeps holding. That
    invariant is what lets the Day 4 load test prove correctness with a `COUNT`
    and a `SUM` instead of trusting the application to have been right.
    """
    account_id = await _account_id(session, effect.account_ref)

    current = (
        await session.execute(
            select(Account.balance_minor, Account.version).where(Account.id == account_id)
        )
    ).one()

    # Strict `>`: equal sequences mean the same provider state, and re-applying it
    # is at best a no-op and at worst a rewind of anything applied since.
    if effect.sequence <= current.version:
        return EffectResult.SUPERSEDED

    delta = effect.balance_minor - current.balance_minor

    if not await _claim_ledger_row(
        session, account_id=account_id, event_id=event.id, amount_minor=delta
    ):
        return EffectResult.ALREADY_APPLIED

    await session.execute(
        update(Account)
        .where(Account.id == account_id)
        .values(
            balance_minor=effect.balance_minor,
            version=effect.sequence,
            updated_at=func.now(),
        )
    )
    return EffectResult.APPLIED
