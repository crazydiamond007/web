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
from webhook_receiver.adapters.rng import Rng, create_rng
from webhook_receiver.config import Settings, get_settings
from webhook_receiver.domain.balance import registry
from webhook_receiver.domain.handlers import HandlerRegistry
from webhook_receiver.obs.logging import configure_logging
from webhook_receiver.services.process import process_event

log = structlog.get_logger(__name__)


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

        while not shutdown.is_set():
            processed = await poll_once(
                factory, settings=settings, clock=clock, rng=rng, handlers=registry
            )
            if processed == 0:
                # Waiting on the event rather than sleeping makes shutdown
                # immediate instead of taking up to a full poll interval.
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
