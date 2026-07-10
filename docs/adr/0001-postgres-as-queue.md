---
status: accepted
date: 2026-07-10
deciders: backend
consulted: SPEC.md §0, §4, NFR-4, NFR-7
---

# ADR-0001: Postgres as the queue, rather than a dedicated broker

## Context and Problem Statement

Ingestion accepts a webhook, persists it, and returns `200` immediately; a worker processes it later
(SPEC §0, "accept-and-queue"). That split needs a queue. The obvious candidates are a purpose-built
broker — SQS, RabbitMQ, Redis, Kafka — or the Postgres instance we already need for the event and
effect tables.

The requirement that decides it is **NFR-4**: *"A worker that dies mid-processing leaves no
half-applied effect, and the event gets retried cleanly."*

Processing an event does two things that must agree:

1. Apply the business effect (insert a `ledger_entry`).
2. Record that the event is done (set `webhook_event.status = 'succeeded'`, ack the queue).

If those two commit to *different systems*, there is a window between them. A crash inside that
window leaves the effect applied and the event un-acked — so the event is redelivered and the effect
is applied twice — or the event acked and the effect missing, so it is silently lost. This is the
dual-write problem, and no amount of careful ordering removes it. Ordering only chooses **which** of
the two failures you get.

## Considered Options

1. **Postgres as the queue** — `SELECT ... FOR UPDATE SKIP LOCKED` over a `status`/`next_attempt_at`
   index.
2. **A dedicated broker (SQS/Rabbit/Redis)** plus the transactional-outbox pattern.
3. **A dedicated broker, no outbox** — ack after the effect commits, accept the window.
4. **Kafka**, partitioned by entity id, using partition affinity for ordering.

## Decision Outcome

Chosen: **option 1, Postgres as the queue.**

The effect insert and the queue-state update are the same transaction against the same database.
There is no window. Either both commit or neither does, enforced by the one thing already responsible
for our durability guarantee. A worker killed mid-flight — `SIGKILL`, OOM, a severed network — leaves
the row exactly as it was, its advisory lock released by the transaction abort, and the event is
picked up again on the next poll with no cleanup path to get wrong.

Postgres supplies every primitive the design needs, and they compose because they are all in the same
transaction:

| Need | Primitive | Requirement |
|---|---|---|
| Two workers never claim one row | `FOR UPDATE SKIP LOCKED` | FR-7 |
| Events for one entity serialise | `pg_advisory_xact_lock(hash(entity))` | FR-9 |
| A redelivery inserts nothing | `UNIQUE (source, idempotency_key)` | FR-5 |
| An event has at most one effect | `UNIQUE (event_id)` on the ledger | FR-6 |
| Delayed retry | `next_attempt_at > now()`, one index | FR-12 |

Note the second row. It is the reason a broker does not simply drop in: SQS gives you at-most-one
*consumer per message*, which is not the property we need. Two workers holding two **different**
events for the **same** account will both proceed, and both will read the same balance. `SKIP LOCKED`
does not help — the rows differ, so neither is skipped. Per-entity serialisation is a *database* lock,
not a queue feature, and once you need the database for that, the broker is buying less than it costs.

### Why not the alternatives

**Option 2 (broker + outbox).** Correct, and the standard answer when the queue must be external. It
reintroduces exactly what we removed: an outbox table, a relay process to drain it, at-least-once
delivery *from the relay*, and therefore a second dedup layer at the consumer. That is more moving
parts, more failure modes, and more code, to arrive back at the guarantee a single transaction gives
for free. Right call when you have multiple services consuming the stream. We have one.

**Option 3 (broker, no outbox).** This is the dual-write problem accepted as a design. Concretely:
the worker inserts the `ledger_entry`, commits, then crashes before acking SQS. The message
reappears, the ledger row is re-inserted, and the account is credited twice — the precise bug this
service exists to prevent. Rejected.

**Option 4 (Kafka).** Partitioning by entity id gives ordering per partition without an advisory
lock, which is elegant. But it does not give idempotency: consumer offsets commit separately from the
effect, so it is option 3 with better ordering. It also fixes the parallelism ceiling at the
partition count, makes a single hot account a hot partition, and adds a stateful cluster to a service
whose current requirement is one `POST` endpoint and a worker. Reconsider at a volume we do not have.

## Consequences

**Good.**

- NFR-4 holds by construction. No distributed transaction, no outbox, no relay, no second dedup layer.
- One datastore to run, back up, and reason about. The whole system state is queryable with `SELECT`,
  which is most of NFR-5: an operator can trace any event's state changes from the tables alone.
- Tests are honest. `SKIP LOCKED`, advisory locks, and unique constraints are database behaviours, so
  the integration suite runs them against a real Postgres (SPEC §6.5) rather than against a mock of a
  broker's semantics.
- Retry scheduling is a column and a `WHERE` clause, not a dead-letter exchange and a TTL policy.

**Bad, and accepted.**

- **Throughput is bounded by Postgres.** NFR-7 states this: per-entity locking lets different entities
  run in parallel, so adding workers raises throughput *until the database becomes the limit*. That
  limit is real, and it is the ceiling on this design. Day 4 measures where it sits.
- **Polling is not free.** Every idle worker runs a query on `poll_interval_seconds`. The index on
  `(status, next_attempt_at)` keeps it cheap, but it is not the zero-cost push a broker gives.
  `LISTEN`/`NOTIFY` is the escape hatch if polling ever shows up in a profile.
- **The event table grows without bound**, and it is now both the audit log and the queue. NFR-12
  requires a written retention stance; the two roles have different retention needs and that tension
  is real.
- **`SELECT FOR UPDATE SKIP LOCKED` holds a row lock for the duration of processing.** A handler that
  blocks on a slow downstream holds a transaction open, and long transactions block `VACUUM` and bloat
  the table. Handler timeouts are therefore not a nicety; they are a database-health requirement.

## When to revisit

Move to a broker when any of these becomes true:

- Ingestion or processing throughput approaches the measured Postgres ceiling and vertical scaling
  has run out.
- A second consumer needs the same event stream. At that point the outbox stops being overhead and
  starts being the point.
- Retry backlogs grow large enough that the queue's working set competes with the primary workload for
  buffer cache.

Until then, adding a broker would add a failure mode without removing one.
