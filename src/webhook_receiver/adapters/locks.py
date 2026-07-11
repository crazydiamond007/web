"""Per-entity serialisation with a Postgres advisory lock (FR-9).

Two different races, two different mechanisms. It is worth being precise about
which one does what, because they are easy to confuse and neither covers the
other:

* ``FOR UPDATE SKIP LOCKED`` stops two workers claiming **the same row**.
* ``pg_advisory_xact_lock`` stops two workers claiming **two different rows for
  the same entity**.

Without the second one, worker A takes event 1 for account X and worker B takes
event 2 for account X -- different rows, so `SKIP LOCKED` is perfectly happy --
and they then read the same balance, both add to it, and one write lands on top
of the other. `SKIP LOCKED` cannot see that coming; it protects rows, and the
thing that needs protecting is the *account*.

The lock is `_xact_`: it is released when the transaction ends, whether that is a
commit, a rollback, or the connection dying under a worker that was OOM-killed
mid-handler. There is no unlock call to forget and no lock to leak. A
session-scoped advisory lock would need a `finally`, and a `finally` does not run
when the process is shot.
"""

from __future__ import annotations

from hashlib import blake2b

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from webhook_receiver.domain.errors import LockContentionError

# Postgres raises 55P03 (`lock_not_available`) when `lock_timeout` expires while
# waiting for a lock. That is the one DB error here that means "busy, try later"
# rather than "broken".
LOCK_NOT_AVAILABLE = "55P03"

# The advisory lock namespace is a signed 64-bit integer, so the key has to fit
# `bigint` -- not `bigint`'s unsigned cousin, which does not exist in Postgres.
_KEY_BYTES = 8


def advisory_lock_key(entity_type: str, entity_id: str) -> int:
    """Derive a stable signed 64-bit lock key for one business entity.

    Hashed in Python, deliberately, rather than with Postgres' `hashtext()`:
    `hashtext` is an internal function whose output is explicitly not guaranteed
    stable across major versions. If it changed under a rolling upgrade, workers
    on the old and new versions would derive *different* keys for the same
    account, take *different* locks, and serialise nothing -- while every test
    still passed. The failure would be silent, intermittent, and only visible as
    a wrong balance.

    `blake2b` gives us a hash that is pinned by our own dependency versions,
    reproducible on any machine, and assertable in a unit test.

    Two entities can still collide onto one key; at 2^64 keys this is vanishingly
    unlikely, and the consequence is harmless -- two unrelated accounts serialise
    against each other for a moment. Slower, never wrong. The reverse trade would
    be unacceptable.
    """
    # The NUL separator makes the encoding unambiguous: without it, entity
    # ("account", "1x") and ("account1", "x") would hash to the same key.
    digest = blake2b(f"{entity_type}\x00{entity_id}".encode(), digest_size=_KEY_BYTES).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


async def lock_entity(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: str,
    timeout_seconds: float,
) -> None:
    """Serialise on one entity until this transaction ends.

    Blocks while another worker holds the entity, then gives up after
    `timeout_seconds` and raises ``LockContentionError`` -- retryable, because a
    busy entity is a fact about the world, not about the event (FR-11).

    A bounded wait rather than an unbounded one: with `pg_advisory_xact_lock` and
    no timeout, a worker stuck on a slow handler would hold every other worker on
    that entity indefinitely, and the queue would stall behind one hot account
    with no error, no metric, and nothing in the logs -- just silence.
    """
    timeout_ms = int(timeout_seconds * 1000)

    # `set_config(..., is_local => true)` is `SET LOCAL` with bind parameters,
    # which `SET` itself does not accept. Scoped to this transaction, so it
    # cannot leak onto the next event that borrows this pooled connection.
    await session.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": f"{timeout_ms}ms"},
    )

    key = advisory_lock_key(entity_type, entity_id)
    try:
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    except DBAPIError as exc:
        if _is_lock_timeout(exc):
            msg = (
                f"gave up after {timeout_seconds}s waiting for {entity_type}:{entity_id}; "
                f"another worker is holding it"
            )
            raise LockContentionError(msg) from exc
        # Any other database error is not contention and must not be laundered
        # into a retryable one. Let it out.
        raise


def _is_lock_timeout(exc: DBAPIError) -> bool:
    """Is this the `lock_timeout` firing, or a real database failure?

    Matched on SQLSTATE rather than on the driver's exception class or its
    message: the code is part of the Postgres wire protocol, so it survives a
    driver upgrade and a server locale that translates the message text.
    """
    sqlstate: object = getattr(exc.orig, "sqlstate", None)
    return sqlstate == LOCK_NOT_AVAILABLE
