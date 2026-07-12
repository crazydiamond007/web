"""The worker's poll loop must outlive its database.

The database is the worker's *input*. It goes away sometimes -- a failover, a
restart, a private-network hiccup on a PaaS -- and a worker that exits when it
does stops draining the queue while the API cheerfully carries on returning 202.
Nothing about that is visible from the outside: health checks are green, every
delivery is durably stored, and not one event is ever processed.

Compose hid this for a long time. `depends_on: service_healthy` meant Postgres was
always up before the worker started, and `restart: unless-stopped` restarted it
forever if it wasn't. A platform with neither -- which is most of them -- would
have found it the hard way.
"""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING

import pytest

from webhook_receiver.worker import main

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from webhook_receiver.adapters.clock import Clock
    from webhook_receiver.adapters.rng import Rng
    from webhook_receiver.domain.handlers import HandlerRegistry

from webhook_receiver.config import Settings


@pytest.fixture(autouse=True)
def _schema_already_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about the poll loop, not about startup.

    `run()` now waits for the schema before it polls, and against the deliberately
    unreachable DSN below that wait would run to its full timeout. The startup path
    has its own suite, against a real database: tests/integration/test_worker_startup.py.
    """

    async def ready(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main, "await_schema", ready)


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@unreachable.invalid:5432/db",
        admin_api_key="k",
        # Tiny, so a backoff that is exercised for real still runs in milliseconds.
        backoff_base_seconds=0.001,
        backoff_cap_seconds=0.001,
        poll_interval_seconds=0.001,
        jitter_seed=1,
        _env_file=None,
    )


async def test_a_transient_database_failure_does_not_kill_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = asyncio.Event()
    attempts = 0

    async def flaky_poll(
        factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        clock: Clock,
        rng: Rng,
        handlers: HandlerRegistry,
    ) -> int:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            # Exactly what asyncpg raises when the host stops resolving.
            raise socket.gaierror(-2, "Name or service not known")
        shutdown.set()
        return 0

    monkeypatch.setattr(main, "poll_once", flaky_poll)

    await main.run(_settings(), shutdown)

    # It waited the outage out and came back, rather than exiting on the first one.
    assert attempts == 3


async def test_an_unclassified_failure_still_kills_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A TypeError is a bug in our own code. It will not be better on the fourth
    # attempt, and swallowing it into a retry loop would bury the stack trace
    # instead of surfacing it. Dying is the correct, loud answer -- that is what a
    # platform's restart policy is for.
    async def buggy_poll(
        factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        clock: Clock,
        rng: Rng,
        handlers: HandlerRegistry,
    ) -> int:
        msg = "a bug in our own code"
        raise TypeError(msg)

    monkeypatch.setattr(main, "poll_once", buggy_poll)

    with pytest.raises(TypeError, match="a bug in our own code"):
        await main.run(_settings(), asyncio.Event())


async def test_a_pending_shutdown_cuts_a_long_backoff_short() -> None:
    """A worker mid-backoff must still stop when the platform says stop.

    The backoff cap is 300s by default. `asyncio.sleep`-ing through it would
    outlast any termination grace period a platform gives us, so the process would
    be SIGKILLed rather than drained -- and a worker killed mid-transaction is the
    one thing NFR-4 promises cannot happen. Waiting on the *event* is what makes
    the delay interruptible.
    """
    shutdown = asyncio.Event()
    shutdown.set()

    loop = asyncio.get_running_loop()
    started = loop.time()
    await main._wait(shutdown, 300.0)  # the interruptibility IS the unit under test
    elapsed = loop.time() - started

    # A plain sleep would take five minutes.
    assert elapsed < 1.0
