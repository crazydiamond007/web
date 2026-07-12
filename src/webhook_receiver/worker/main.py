"""Worker entrypoint: poll for due events and process them (FR-7).

The loop is deliberately dull. It reads a batch of due event ids, hands each one
to `services.process`, and sleeps only when there was nothing to do. Everything
that is hard -- the claim, the entity lock, exactly-once, ordering -- happens one
layer down, in a single transaction per event, and this file does not need to
know about any of it.

Scaling is horizontal and requires no coordination: run four of these and
`FOR UPDATE SKIP LOCKED` keeps them off each other's rows, while the advisory lock
keeps them off each other's *entities*. There is no leader, no partition
assignment, and no shared state beyond the database.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import structlog
from prometheus_client import start_http_server
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from webhook_receiver.adapters import queue
from webhook_receiver.adapters.clock import Clock, SystemClock
from webhook_receiver.adapters.database import create_engine, create_session_factory, session_scope
from webhook_receiver.adapters.failures import is_retryable
from webhook_receiver.adapters.rng import Rng, create_rng
from webhook_receiver.config import Settings, get_settings
from webhook_receiver.domain.backoff import next_delay_seconds
from webhook_receiver.domain.balance import registry
from webhook_receiver.domain.handlers import HandlerRegistry
from webhook_receiver.obs.logging import configure_logging
from webhook_receiver.services.process import process_event

log = structlog.get_logger(__name__)


async def _wait(shutdown: asyncio.Event, seconds: float) -> None:
    """Sleep for `seconds`, or wake immediately if a shutdown is requested.

    Waiting on the event rather than sleeping makes shutdown prompt instead of
    taking up to a full delay -- which matters most when the delay is a long
    backoff and the platform is counting down to a SIGKILL.
    """
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)


async def poll_once(
    factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    clock: Clock,
    rng: Rng,
    handlers: HandlerRegistry,
) -> int:
    """Process one batch of due events. Returns how many ids we looked at.

    The ids are read in their own short transaction, which is then closed *before*
    any processing starts. Holding it open across the batch would keep a snapshot
    -- and a connection -- alive for as long as the slowest handler in it, and
    would couple events that have nothing to do with each other.
    """
    async with session_scope(factory) as session:
        event_ids = await queue.due_event_ids(
            session, now=clock.now(), limit=settings.poll_batch_size
        )

    for event_id in event_ids:
        # Each event gets its own transaction, so one poison event cannot roll
        # back the work of the events either side of it in the batch.
        structlog.contextvars.clear_contextvars()
        await process_event(
            factory,
            event_id=event_id,
            registry=handlers,
            settings=settings,
            clock=clock,
            rng=rng,
        )

    structlog.contextvars.clear_contextvars()
    return len(event_ids)


async def run(settings: Settings, shutdown: asyncio.Event) -> None:
    """Poll until asked to stop, then drain.

    `shutdown` rather than a `while True` with a KeyboardInterrupt: a worker that
    is killed mid-transaction must leave no half-applied effect (NFR-4), so the
    loop needs a cooperative exit between units of work, not a signal delivered
    into the middle of one.

    A full batch means the queue is backed up, so we come straight back for more
    instead of sleeping. Only an empty poll waits.

    The database is this process's *input*, and it will go away sometimes -- a
    failover, a restart, a private-network hiccup on a PaaS. A worker that exits
    when that happens stops draining the queue while the API cheerfully carries on
    accepting events, so a transient database failure is waited out here rather
    than allowed to kill the process.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    clock = SystemClock()
    # Seeded only if JITTER_SEED is set, which is a test and debugging affordance.
    # Seeding every worker in production would give the whole fleet the *same*
    # jitter, which is precisely the lockstep that jitter exists to break.
    rng = create_rng(settings.jitter_seed)

    try:
        log.info(
            "worker.started",
            poll_interval_seconds=settings.poll_interval_seconds,
            poll_batch_size=settings.poll_batch_size,
            max_attempts=settings.max_attempts,
            event_types=sorted(registry.event_types),
        )
        if settings.jitter_seed is not None:
            # Loud, because a seeded RNG in production is a correctness problem
            # that looks like nothing until the retries synchronise.
            log.warning("worker.jitter_seeded", seed=settings.jitter_seed)

        consecutive_failures = 0

        while not shutdown.is_set():
            try:
                processed = await poll_once(
                    factory, settings=settings, clock=clock, rng=rng, handlers=registry
                )
            except Exception as exc:
                # Only failures the taxonomy *recognises* are waited out. An
                # unclassified exception is far more likely to be a bug in our own
                # code than the world's weather, and looping on a bug forever would
                # bury the stack trace instead of surfacing it. That one still kills
                # the process, loudly -- which is what a restart policy is for.
                if not is_retryable(exc):
                    raise

                consecutive_failures += 1
                # The same full-jitter schedule the events themselves retry on
                # (FR-12), for the same reason it exists there. When a database
                # comes back, every worker in the fleet is sitting in this branch,
                # and a fixed delay would have all of them reconnect on the same
                # tick -- re-flooring a server that has just got to its feet.
                delay = next_delay_seconds(
                    attempt=consecutive_failures,
                    base_seconds=settings.backoff_base_seconds,
                    cap_seconds=settings.backoff_cap_seconds,
                    rng=rng,
                )
                log.warning(
                    "worker.poll_failed",
                    error_class=type(exc).__name__,
                    consecutive_failures=consecutive_failures,
                    retry_in_seconds=round(delay, 3),
                )
                await _wait(shutdown, delay)
                continue

            if consecutive_failures:
                log.info("worker.recovered", after_failures=consecutive_failures)
                consecutive_failures = 0

            if processed == 0:
                await _wait(shutdown, settings.poll_interval_seconds)
    finally:
        await engine.dispose()
        log.info("worker.stopped")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)


async def _main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, environment=settings.environment)

    # FR-19. The worker owns the `processed`, `retried` and `dead_lettered`
    # counters -- they are incremented in *this* process, and Prometheus scrapes a
    # process, not an application. Without a server here they would be invisible:
    # the app's /metrics would report only ingestion, and the half of the pipeline
    # where events actually fail would have no telemetry at all.
    #
    # A daemon thread with its own tiny HTTP server, which is what
    # `prometheus_client` gives us. It does not touch the event loop and it dies
    # with the process.
    start_http_server(settings.worker_metrics_port)
    log.info("worker.metrics_listening", port=settings.worker_metrics_port)

    shutdown = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), shutdown)
    await run(settings, shutdown)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
