"""Translating driver exceptions into the domain's error taxonomy (FR-11).

The domain says what *kinds* of failure exist (`domain/errors.py`). This is where
a concrete `asyncpg.PostgresError` wrapped in a `sqlalchemy.exc.DBAPIError` gets
mapped onto one of them. It lives in the adapter layer because it is the only
place allowed to know that SQLAlchemy exists.

SPEC §6.6 is the rule: **"anything unclassified is treated as non-retryable and
dead-lettered."** So the default here is `False`, and retryability is something a
failure has to *earn* by being recognised.

That default has teeth, and it points at the right thing. An exception we cannot
classify is most likely a bug in our own code -- a `TypeError`, a `KeyError` --
and a bug does not get better on the fourth attempt. Retrying it wastes the
budget, delays every event behind it, and buries the stack trace under four
identical copies of itself. Dead-lettering it immediately puts it in front of a
human, which is the only thing that will actually fix it.

But the default is only *safe* if the genuinely transient failures are recognised
explicitly. A database failover is not a bug in our code, and if it fell through
to the default it would dead-letter every event in flight -- turning a fifteen-
second blip into a manual replay of thousands of events. So the transient cases
are enumerated below, by SQLSTATE, and they are the reason this file exists
rather than a one-line `isinstance` check at the call site.
"""

from __future__ import annotations

import socket

from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from webhook_receiver.domain.errors import NonRetryableError, RetryableError

# Postgres SQLSTATEs that mean "the world was briefly uncooperative", not "your
# statement is wrong". Matched on the code rather than the message: the code is
# part of the wire protocol, so it survives a driver upgrade and a server whose
# locale translates the message text.
RETRYABLE_SQLSTATES = frozenset(
    {
        "40001",  # serialization_failure   -- a concurrent transaction won; retry is the cure
        "40P01",  # deadlock_detected       -- Postgres shot one of us; the survivor committed
        "55P03",  # lock_not_available      -- lock_timeout fired (our advisory lock)
        "53300",  # too_many_connections    -- the pool is saturated, briefly
        "57P01",  # admin_shutdown          -- a failover, or someone ran pg_ctl restart
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now      -- the server is still starting up
        "08000",  # connection_exception
        "08003",  # connection_does_not_exist
        "08006",  # connection_failure
    }
)


def is_retryable(exc: BaseException) -> bool:
    """Can trying this again plausibly succeed?

    The order matters: the domain's own classification always wins, because a
    handler that says "this is permanent" knows more about its event than any
    driver heuristic can.
    """
    # The taxonomy the domain declared for itself.
    if isinstance(exc, NonRetryableError):
        return False
    if isinstance(exc, RetryableError):
        return True

    # The pool could not hand out a connection in time. Nothing to do with the
    # event; everything to do with load right now.
    if isinstance(exc, SQLAlchemyTimeoutError):
        return True

    if isinstance(exc, DBAPIError):
        # SQLAlchemy sets this when the connection died under the statement --
        # a failover, a killed backend, a severed TCP connection. The statement
        # never ran, so running it again is exactly the right move.
        if exc.connection_invalidated:
            return True
        sqlstate: object = getattr(exc.orig, "sqlstate", None)
        return sqlstate in RETRYABLE_SQLSTATES

    # A socket-level failure that never reached SQLAlchemy's wrapping is transient.
    #
    # `socket.gaierror` is named explicitly because it is the one that gets missed:
    # it is an `OSError` but *not* a `ConnectionError`, so it slips past the obvious
    # `ConnectionError` check and falls through to the default. It is what asyncpg
    # raises -- raw and unwrapped, never reaching SQLAlchemy's DBAPIError layer --
    # when the database's hostname stops resolving. That is not an exotic case: a
    # managed Postgres cycling a container, or a private-network DNS blip on a PaaS,
    # produces exactly this, and the connection fails before there is a connection
    # to have an error *about*. Left unclassified, a fifteen-second DNS wobble would
    # dead-letter every event in flight -- the very outcome the SQLSTATEs above are
    # enumerated to prevent.
    #
    # Everything else falls through as unclassified -- and SPEC §6.6 says an
    # unclassified failure is non-retryable. We do not know what it is, so we do
    # not retry it: an unrecognised exception is far more likely to be our bug
    # than the world's weather, and a human should see it now rather than in five
    # attempts' time.
    return isinstance(exc, ConnectionError | TimeoutError | socket.gaierror)
