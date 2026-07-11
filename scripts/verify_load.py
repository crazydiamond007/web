#!/usr/bin/env python
"""Grade the load test from the database (NFR-1).

Deliberately a separate process that trusts nothing the load generator said. It
waits for the queue to drain, then asks Postgres four questions:

1. **Were duplicates absorbed?**  requests sent >> events stored.
2. **Was every event applied exactly once?**  `count(ledger_entry) == count(distinct events)`
   -- the headline claim, stated exactly as SPEC §7 states it.
3. **Does the money add up?**  `account.balance_minor == SUM(ledger_entry.amount_minor)`
   for every account -- `drift = 0` in `v_account_reconciliation`.
4. **Is the balance the one arithmetic predicts?**  Every event credits the same
   amount, so an account's balance must be `AMOUNT x (its distinct events)`. This
   is the check that would catch an effect applied twice *and* a compensating bug
   that hid it.

(2) and (3) are not the same test, and a system could pass either alone. (2) counts
rows; (3) checks that the cached balance agrees with them. Only (4) pins the
absolute number, which is what "no double-processing" actually means to a user.

Exits non-zero if any of them fails, so CI and `make load` can depend on it.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass

import asyncpg

from webhook_receiver.config import get_settings

DRAIN_POLL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str


def _dsn() -> str:
    """asyncpg wants a plain postgres:// DSN, not SQLAlchemy's +asyncpg form."""
    return str(get_settings().database_url).replace("postgresql+asyncpg://", "postgresql://")


async def _drain(conn: asyncpg.Connection, *, budget_seconds: float) -> tuple[bool, float]:
    """Wait until the workers have nothing left to do.

    The load test measures *ingestion*, which returns before processing happens
    (FR-2). So the effects are still landing when locust exits, and asserting on
    them immediately would be asserting on a race.
    """
    started = time.monotonic()
    while time.monotonic() - started < budget_seconds:
        pending = await conn.fetchval("SELECT count(*) FROM webhook_event WHERE status = 'pending'")
        if pending == 0:
            return True, time.monotonic() - started
        await asyncio.sleep(DRAIN_POLL_SECONDS)
    return False, time.monotonic() - started


async def _checks(conn: asyncpg.Connection, *, amount: int) -> list[Check]:
    events = await conn.fetchval("SELECT count(*) FROM webhook_event")
    distinct = await conn.fetchval(
        "SELECT count(DISTINCT (source, idempotency_key)) FROM webhook_event"
    )
    ledger_rows = await conn.fetchval("SELECT count(*) FROM ledger_entry")
    succeeded = await conn.fetchval("SELECT count(*) FROM webhook_event WHERE status = 'succeeded'")
    dead = await conn.fetchval("SELECT count(*) FROM webhook_event WHERE status = 'dead_lettered'")

    checks = [
        Check(
            "every stored event is unique",
            events == distinct,
            f"{events} rows, {distinct} distinct (source, idempotency_key)",
        ),
        Check(
            "nothing was dead-lettered",
            dead == 0,
            f"{dead} dead-lettered",
        ),
        Check(
            "every event was processed",
            succeeded == events,
            f"{succeeded} of {events} succeeded",
        ),
        # THE claim. SPEC §7: "assert count(ledger_entry) == count(distinct events)".
        Check(
            "count(ledger_entry) == count(distinct events)",
            ledger_rows == distinct,
            f"{ledger_rows} ledger rows, {distinct} distinct events",
        ),
    ]

    drift = await conn.fetch("SELECT external_ref, drift FROM v_account_reconciliation")
    drifted = [row for row in drift if row["drift"] != 0]
    checks.append(
        Check(
            "balance == SUM(ledger) on every account",
            not drifted,
            f"{len(drift)} accounts, {len(drifted)} with drift",
        )
    )

    # The absolute number, not just the internal consistency of two counts.
    wrong = await conn.fetch(
        """
        SELECT a.external_ref, a.balance_minor, $1::bigint * count(e.id) AS expected
        FROM account a
        JOIN webhook_event e ON e.entity_id = a.external_ref
        GROUP BY a.id, a.external_ref, a.balance_minor
        HAVING a.balance_minor <> $1::bigint * count(e.id)
        """,
        amount,
    )
    checks.append(
        Check(
            "balance == amount x distinct events, per account",
            not wrong,
            "every balance is exactly what arithmetic predicts"
            if not wrong
            else f"{len(wrong)} accounts wrong, e.g. {dict(wrong[0])}",
        )
    )

    return checks


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amount", type=int, default=100, help="LOAD_AMOUNT used by the run.")
    parser.add_argument("--requests", type=int, default=0, help="Deliveries locust sent.")
    parser.add_argument(
        "--drain-timeout", type=float, default=300.0, help="Seconds to wait for the queue."
    )
    args = parser.parse_args()

    conn: asyncpg.Connection = await asyncpg.connect(_dsn())
    try:
        print("waiting for the workers to drain the queue...")
        drained, seconds = await _drain(conn, budget_seconds=args.drain_timeout)
        if not drained:
            pending = await conn.fetchval(
                "SELECT count(*) FROM webhook_event WHERE status = 'pending'"
            )
            print(f"FAILED: queue still has {pending} pending events after {seconds:.0f}s")
            return 1
        print(f"drained in {seconds:.1f}s\n")

        events = await conn.fetchval("SELECT count(*) FROM webhook_event")
        if events == 0:
            print("FAILED: no events in the database -- did the load test run?")
            return 1

        if args.requests:
            absorbed = args.requests - events
            print(
                f"{args.requests} deliveries sent -> {events} events stored "
                f"({absorbed} duplicates absorbed, {absorbed / args.requests:.0%})\n"
            )

        checks = await _checks(conn, amount=args.amount)
    finally:
        await conn.close()

    width = max(len(check.name) for check in checks)
    for check in checks:
        mark = "PASS" if check.passed else "FAIL"
        print(f"  [{mark}] {check.name.ljust(width)}  {check.detail}")

    failed = [check for check in checks if not check.passed]
    if failed:
        print(f"\n{len(failed)} CHECK(S) FAILED -- there is double-processing.")
        return 1

    print("\nzero double-processing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
