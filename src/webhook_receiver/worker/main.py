"""Worker entrypoint.

Day 0 wires the process, its configuration, and its shutdown. The poll loop
itself -- `SELECT ... FOR UPDATE SKIP LOCKED` plus `pg_advisory_xact_lock` --
lands in the Day 2 processing slice (FR-7..FR-10), where it can be tested
against a real Postgres rather than asserted about.

Until then this process starts, proves it can reach the database, and idles on
the configured poll interval, so `docker compose up` brings up the real topology
(app + worker + postgres) rather than a topology we intend to have later.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import structlog

from webhook_receiver.adapters.database import create_engine
from webhook_receiver.config import Settings, get_settings
from webhook_receiver.obs.logging import configure_logging

log = structlog.get_logger(__name__)


async def run(settings: Settings, shutdown: asyncio.Event) -> None:
    """Poll until asked to stop, then drain.

    `shutdown` rather than a `while True` with a KeyboardInterrupt: a worker that
    is killed mid-transaction must leave no half-applied effect (NFR-4), so the
    loop needs a cooperative exit between units of work, not a signal delivered
    into the middle of one.
    """
    engine = create_engine(settings)
    try:
        log.info("worker.started", poll_interval_seconds=settings.poll_interval_seconds)
        while not shutdown.is_set():
            # TODO(day-2): claim a batch with FOR UPDATE SKIP LOCKED and dispatch.
            # Waiting on the event (rather than sleeping) makes shutdown immediate.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown.wait(), timeout=settings.poll_interval_seconds)
    finally:
        await engine.dispose()
        log.info("worker.stopped")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)


async def _main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, environment=settings.environment)

    shutdown = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), shutdown)
    await run(settings, shutdown)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
