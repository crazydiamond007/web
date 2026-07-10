# Idempotent Webhook Receiver

A webhook receiver that stays correct under the things that actually go wrong with at-least-once
delivery: duplicate deliveries, events arriving out of order, partial failures, and downstream
timeouts.

> **Status: Day 0 (foundation) — in progress.** The schema, configuration, health probes, container
> and CI are in place. Ingestion, the worker, and the retry/DLQ machinery land in Days 1–3; the load
> test that produces the headline number lands on Day 4. Sections describing unbuilt behaviour are
> marked **(not yet implemented)**. Nothing in this README claims a result that has not been
> measured. See [Current status](#current-status).

---

## The problem

Providers promise **at-least-once** delivery, not exactly-once. Exactly-once is not achievable over
an unreliable network, so nobody offers it. The provider sends an event, waits for a `2xx`, and
resends if it doesn't get one before its timeout.

The awkward part is the case everyone forgets. The provider's timeout can fire *after* we have
already processed the event — we crashed, or were slow, before the response made it back. The
provider cannot distinguish "never processed" from "processed, but the ack was lost." So it resends
either way.

If the event was `charge.succeeded`, a naive receiver credits the account twice.

So the entire design answers one question: **how do we make a redelivery safe to accept?**

## The two answers

### 1. Accept-and-queue ingestion

Verify the signature, write the raw event to Postgres, return `200`. The actual work happens later,
in a worker.

- Ingestion is a single insert, so it is fast and its latency is flat.
- A slow or hung downstream never makes the provider wait on us, so the provider never times out and
  never resends for that reason.
- "We received it" is kept separate from "we processed it," which is what lets the two fail
  independently.

### 2. Idempotency lives in the database, not in application code

This is the part that is easy to get wrong, and the wrong version passes every test you write for it
until it meets concurrency.

The tempting implementation is:

```python
if not db.query(Event).filter_by(id=event_id).first():   # ← check
    db.add(Event(id=event_id))                           # ← then act
```

Two workers run the `SELECT` at the same time, both see nothing, both insert. The check-then-act is
not atomic, so under exactly the conditions it exists to handle — concurrent redelivery — it does
nothing at all.

Instead, correctness comes from constraints Postgres enforces atomically:

| Constraint | Table | Guarantees |
|---|---|---|
| `UNIQUE (source, idempotency_key)` | `webhook_event` | A redelivery inserts zero new rows (FR-5) |
| `UNIQUE (event_id)` | `ledger_entry` | An event can produce **at most one** effect, ever (FR-6) |
| `UNIQUE (event_id)` | `dead_letter_entry` | One DLQ row per event |
| `UNIQUE (event_id, attempt_number)` | `processing_attempt` | Attempt numbers cannot collide |

We attempt the insert and let Postgres arbitrate. There is no read-then-write anywhere in the
ingestion or effect path.

---

## Failure modes, and the mechanism for each

Every mechanism below exists because of a specific way this service can break. That is the only
reason any of them are here.

### The provider redelivers an event we already processed

**Mechanism:** `UNIQUE (source, idempotency_key)` on `webhook_event`.

The second delivery loses the race inside Postgres, atomically. Ingestion catches the unique
violation and still returns `200` — because from the provider's point of view the event *is*
accepted, and returning an error would only cause it to retry again.

The dedup key is `(source, idempotency_key)`, not `external_id` alone: two different providers may
legitimately issue the same event id. `idempotency_key` defaults to the provider's event id and can
be overridden with an `Idempotency-Key` header.

### A worker crashes halfway through processing

**Mechanism:** the effect insert and the status update share **one transaction**.

Either both commit or neither does. There is no window in which the ledger has moved but the event
still looks unprocessed, or vice versa. A worker killed mid-flight leaves the row exactly as it was,
and the event is retried cleanly (NFR-4).

This is the main reason the queue lives in Postgres rather than in a dedicated broker. With a
separate broker you need a distributed transaction or an outbox to get the same property. With one
datastore you get it for free. The cost — throughput is bounded by Postgres — is accepted explicitly
(NFR-7). See [ADR-0001](docs/adr/).

### Two workers grab the same event

**Mechanism:** `SELECT ... FOR UPDATE SKIP LOCKED`. *(not yet implemented — Day 2)*

Each worker claims a batch of due rows, skipping any row another worker already holds. No row is
handed to two workers, and no worker blocks waiting for another's rows.

### Two workers process *different* events for the *same* account

`SKIP LOCKED` does not help here — the rows are different, so neither is skipped. Both workers
proceed, and both read the same account balance.

**Mechanism:** `pg_advisory_xact_lock(hash(entity))`. *(not yet implemented — Day 2)*

Events for one entity serialise; events for different entities still run in parallel, so adding
workers still raises throughput. The lock is transaction-scoped, so it is released on commit *or* on
crash, with no cleanup path to get wrong.

### An older event arrives after a newer one

Ordering is a different problem from concurrency, and it needs a different mechanism. Serialising
two events does not tell you which one *should* win.

**Mechanism:** an optimistic-version guard on `account.version`, using each event's `occurred_at`
and `provider_sequence`. *(not yet implemented — Day 2)*

The newer state wins. The stale event is recorded as **superseded** rather than applied — it is not
an error, and it must not page anyone. (Recording that honestly required a fourth value in the
`attempt_outcome` enum, which `SPEC.md` §3 did not have. See [ADR-0006](docs/adr/0006-superseded-attempt-outcome.md).)

Note that a pure credit/debit ledger is *additive*, and addition commutes — order genuinely does not
matter. The version guard only earns its keep for absolute state writes, which is why the demo
domain has both kinds of handler.

### The downstream is timing out

**Mechanism:** an error taxonomy plus exponential backoff with full jitter. *(not yet implemented — Day 3)*

Failures are classified retryable (timeout, downstream `5xx`, lock contention) or non-retryable (bad
payload, business-rule violation). Anything unclassified is treated as non-retryable and
dead-lettered, so a bug can never become an infinite retry loop.

Retryable failures are rescheduled at `delay = min(cap, base * 2^attempt)`, then **full jitter**:
`random(0, delay)`. Without the jitter, a batch of events that failed together retries together,
and the thundering herd knocks the downstream over again the moment it recovers.

The jitter RNG is seedable so tests are deterministic. It is left unseeded in production, because
seeding it would re-synchronise exactly the herd the jitter exists to break up.

### An event can never succeed

**Mechanism:** bounded attempts, then a dead-letter entry. *(not yet implemented — Day 3)*

A poison event stops holding up healthy traffic. The DLQ row keeps the failure reason, the attempt
count, and the original context, and carries a lifecycle (`needs_review → replaying → resolved |
discarded`) so an operator can triage it.

### We need to reprocess something

**Mechanism:** replay goes back through the *same* dedup, lock, and ledger path. *(not yet implemented — Day 3)*

Replay is not a special path with its own rules — that is how replay endpoints cause the incidents
they were built to fix. Because it reuses `UNIQUE (event_id)` on the effect table, replaying an
already-processed event provably adds zero effects.

### A retryable failure must not need its own status

There is deliberately **no `retrying` state** in `webhook_status`. A retryable failure returns the
event to `pending` with `next_attempt_at` set in the future. One predicate —
`status = 'pending' AND next_attempt_at <= now()` — and one index serve both first attempts and
retries.

---

## The number

> **Not yet measured.** The Day 4 load test replays ≥10,000 duplicate deliveries under concurrency
> and asserts `count(ledger_entry) == count(distinct events)`.
>
> This section will state: *"Replayed 10,000 duplicate deliveries; zero double-processing; ingestion
> p99 = X ms; sustained Y events/s"* — with X and Y filled in from
> [`tests/load/`](tests/load/), reproducible by anyone who clones the repo.
>
> It is left blank rather than filled with a plausible-looking guess.

Note that the assertion `count(ledger_entry) == count(distinct events)` holds over events that are
neither superseded nor dead-lettered, since those produce no ledger row by design. The load test
replays additive, non-poison events, where it holds exactly.

---

## Architecture

Layered, with dependencies pointing inward. The domain layer never imports the framework.

```
  HTTP (FastAPI)        →  api/        routing, schemas, auth, signature
  Application services  →  services/   ingest, process, retry, dlq, replay
  Domain                →  domain/     event model, error taxonomy, backoff, handlers
  Adapters              →  adapters/   SQLAlchemy repositories, advisory lock, clock, rng
  Worker                →  worker/     poll loop (FOR UPDATE SKIP LOCKED) + dispatch
  Cross-cutting         →  obs/        structlog, correlation id, metrics
```

```
receive → verify signature → idempotent persist (UNIQUE) → 200
                                        │
                          worker poll (SKIP LOCKED, due events)
                                        │
                    pg_advisory_xact_lock(hash(entity))   ← per-entity serialise
                                        │
                         dispatch handler → effect (UNIQUE event_id)
                              │                      │
                        success → succeeded    failure
                                                     │
                              retryable? ── yes → schedule backoff + jitter
                                    │
                                   no / attempts exhausted → dead_letter_entry
                                                                      │
                                                        replay (admin) → same path
```

Full detail in [`SPEC.md`](SPEC.md). `ARCHITECTURE.md` arrives with the Day 4 slice.

---

## Running it

Requires Docker (for the stack, and for the integration tests, which use Testcontainers).

```bash
cp .env.example .env          # then edit; nothing has a usable default secret
docker compose up --build
```

Compose brings up `postgres` → `migrate` → `app` + `worker`. Migrations run as a **separate one-shot
service**, not on app startup: two app replicas racing `alembic upgrade head` is a real way to
corrupt a schema.

```bash
curl localhost:8000/healthz   # {"status":"alive"}
curl localhost:8000/readyz    # {"status":"ready","database":"ok"}
```

`/healthz` is liveness and deliberately touches nothing external — if it checked the database, a
database blip would make Kubernetes restart every healthy app pod, escalating a partial outage into a
total one. `/readyz` is readiness and does check the database, because an app that cannot write an
event should be drained from the load balancer rather than accept deliveries it will drop.

### Tests

```bash
uv sync --extra dev
uv run pytest tests/unit          # fast, no Docker
uv run pytest tests/integration   # real Postgres 16 via Testcontainers
uv run pytest                     # everything, with the coverage gate
```

Idempotency, advisory locks, and `SKIP LOCKED` are *database behaviours*. Mocking them would test the
mock, so the integration suite runs against a real Postgres. It **fails rather than skips** when
Docker is absent — a suite that quietly skips itself keeps CI green while the guarantees go
unverified. (`ALLOW_SKIP_INTEGRATION=1` to opt out locally.)

### Checks

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy                       # --strict
```

---

## Configuration

Every threshold lives in [`config.py`](src/webhook_receiver/config.py) with a validation bound, and
has a matching entry in [`.env.example`](.env.example). There are no magic numbers in the code
(NFR-11).

| Setting | Default | Why it exists |
|---|---|---|
| `SIGNATURE_TIMESTAMP_TOLERANCE_SECONDS` | `300` | A captured request cannot be replayed later (FR-4) |
| `MAX_ATTEMPTS` | `5` | Bounds retries before the DLQ (FR-13) |
| `BACKOFF_BASE_SECONDS` | `1.0` | `delay = min(cap, base * 2^attempt)` (FR-12) |
| `BACKOFF_CAP_SECONDS` | `300.0` | Ceiling on the pre-jitter delay |
| `ADVISORY_LOCK_TIMEOUT_SECONDS` | `5.0` | Bounds damage from a stuck transaction (FR-9) |
| `POLL_BATCH_SIZE` | `100` | Rows per `SKIP LOCKED` claim (FR-7) |
| `JITTER_SEED` | unset | Seed for deterministic tests; **never set in production** |

Misconfiguration fails at boot, not at 3am: `backoff_cap < backoff_base` is rejected outright, since
`min(cap, base * 2^n)` would then shorten the *first* retry rather than bound the last.

Secrets are `SecretStr`, so a logged `repr` or a traceback prints `**********` rather than the signing
key. Payloads are never logged in full (NFR-6).

---

## Current status

| Slice | Requirements | State |
|---|---|---|
| Day 0 — foundation | FR-21 | Scaffold, `Settings`, schema migration, `/healthz`, `/readyz`, Docker, CI |
| Day 1 — ingestion | FR-1…FR-6 | Not started |
| Day 2 — processing | FR-7…FR-10 | Not started |
| Day 3 — resilience | FR-11…FR-20 | Not started |
| Day 4 — proof & deploy | NFR-1, NFR-2, NFR-10 | Not started |

Implemented endpoints today: `GET /healthz`, `GET /readyz`. `POST /v1/webhooks/{source}` arrives with
the Day 1 slice.

The schema, however, is complete — all six tables and four enum types from `SPEC.md` §3 ship in
`0001_initial_schema`, with every constraint above already enforced. An integration test asserts the
migration has **zero drift** against the ORM models, and that each unique constraint really does
reject its duplicate.

## Decisions

Architecture decision records live in [`docs/adr/`](docs/adr/) (MADR format):

- **[0001](docs/adr/0001-postgres-as-queue.md)** — Postgres-as-queue vs. a dedicated broker ✅
- **0002** — Advisory locks for per-entity serialisation *(Day 2)*
- **0003** — Idempotent effect via a unique-keyed ledger *(Day 2)*
- **0004** — Retry/backoff parameters and full jitter *(Day 3)*
- **0005** — Accept-and-queue ingestion *(pending)*
- **[0006](docs/adr/0006-superseded-attempt-outcome.md)** — Adding `superseded` to `attempt_outcome`
  ✅ *(a documented deviation from `SPEC.md` §3, with the reasoning)*
