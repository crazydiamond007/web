---
status: accepted
date: 2026-07-12
deciders: backend
consulted: SPEC.md §FR-7, §FR-12, §6.6, §NFR-4
---

# ADR-0009: The worker waits out its database rather than exiting

## Context and Problem Statement

The worker's poll loop reads due events from Postgres. Postgres will, occasionally, not be there: a
failover, a restart, a maintenance window, a DNS blip on a platform's private network. What should
the process do when the query it exists to run cannot run?

This surfaced while preparing the Railway deployment. Pointed at an unreachable database, the worker
raised a bare `socket.gaierror` out of `poll_once`, out of `run()`, out of `asyncio.run`, and exited
with code 1.

Docker Compose had concealed this completely, and did so for four days:

- `depends_on: { postgres: { condition: service_healthy } }` guaranteed Postgres was already
  accepting connections before the worker's first poll.
- `restart: unless-stopped` restarted the worker forever if it ever did die.

Both of those are Compose-specific. A platform with neither — which is most of them — turns the same
code into an outage.

## Decision Drivers

- **A dead worker is invisible.** This is the crux. If the worker exits, the API carries on
  verifying signatures, writing events, and returning `202 Accepted`. `/healthz` stays green. Every
  delivery is durably stored and *nothing is ever processed*. There is no error to page on, because
  from the outside the system looks like it is simply not very busy. You find out from
  `v_queue_health`, or from a customer.
- **Restart budgets are finite.** Railway's `restartPolicyMaxRetries` defaults to 10, and Kubernetes
  backs off to `CrashLoopBackOff`. A worker that crashes on every poll during a 60-second failover
  burns its entire budget in seconds and is then *permanently* dead — long after the database has
  recovered. The blip is transient; the consequence is not.
- **But a bug must still be loud.** The opposite error is a loop that swallows everything and spins
  forever on a `TypeError`, burying the stack trace under a million identical log lines.

## Considered Options

1. **Let it crash; let the platform restart it.** The Erlang answer, and a genuinely good default.
2. **Retry everything in-process, forever.**
3. **Retry the failures we recognise; die on the ones we do not.**

## Decision

**Option 3.**

`run()` classifies a failed poll with the same `is_retryable()` the events themselves are classified
with (ADR-0005). A recognised transient failure is waited out with the same full-jitter backoff. An
unclassified exception is re-raised and kills the process.

```python
try:
    processed = await poll_once(...)
except Exception as exc:
    if not is_retryable(exc):
        raise                       # our bug. Die loudly.
    consecutive_failures += 1
    await _wait(shutdown, next_delay_seconds(attempt=consecutive_failures, ...))
    continue
```

Three things fall out of reusing the existing machinery rather than writing a reconnect loop:

- **The jitter is not decoration.** When a database comes back, *every* worker in the fleet is
  sitting in this branch. A fixed delay would have all of them reconnect on the same tick and
  re-floor a server that has only just got to its feet. The variance is the point — the same
  argument as ADR-0005, applied to the database instead of a downstream.
- **The wait is interruptible.** It waits on the shutdown `Event`, not `asyncio.sleep`. A worker
  sitting in a 300-second backoff must still honour a SIGTERM inside the platform's termination
  grace period, or it gets SIGKILLed — and a worker killed mid-transaction is the one thing NFR-4
  promises cannot happen.
- **"Unclassified is fatal" is the same rule as SPEC §6.6**, which says an unclassified *event*
  failure is dead-lettered rather than retried. Same reasoning, different blast radius: we do not
  know what it is, so we do not loop on it.

## Consequences

**Good.** A failover, a restart, or a network partition is now a gap in throughput rather than an
outage. The worker reconnects on its own, and `worker.poll_failed` / `worker.recovered` say plainly
what happened. The service degrades the way it should: ingestion keeps accepting (events are durable
the moment they are written), processing pauses, and the queue drains when the database returns.

**Bad.** The worker will now sit in a backoff loop indefinitely against a database that is never
coming back — a deleted instance, a revoked credential, a DSN typo'd at deploy time. It logs every
attempt, but it does not exit, so a platform watching only for a crashed process sees a healthy one.
This is a deliberate trade: `due_now` climbing in `v_queue_health` is the signal, and it is the same
signal that catches every other reason the queue stops draining. Alerting on process liveness was
never going to catch this class of failure anyway.

**Consequently, the health of this service is the depth of its queue, not the liveness of its
processes.** That is worth saying out loud, because it is not what a default dashboard measures.

## What this cost to find

`is_retryable()` returned `False` for `socket.gaierror`, which meant even the taxonomy would not have
saved us. It is an `OSError` but *not* a `ConnectionError`, so it slipped past the obvious check and
fell through to the "unclassified ⇒ non-retryable" default. It is also raised *raw* by asyncpg,
before SQLAlchemy has a `DBAPIError` to wrap it in, so none of the carefully enumerated SQLSTATEs
applied either. A DNS name that stops resolving for fifteen seconds would have dead-lettered every
event in flight.

That is the honest reason this ADR exists. The retry taxonomy was written for the *downstream* and
was never once pointed at the database the worker depends on — and the one deployment target that
would have exposed it, Compose, was configured to make sure it never could.
