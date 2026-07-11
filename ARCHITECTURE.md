# Architecture

The whole design follows from one sentence:

> **Correctness is enforced by the database, not by the application.**

Every guarantee this service makes is a constraint you can see in the schema. Delete the Python,
rewrite it in Go, point it at the same Postgres, and it *still* cannot double-process an event. That
is the property being aimed at, and everything below is downstream of it.

---

## The problem, precisely

Webhook providers deliver **at least once**. They resend an event when they don't get a `2xx` in time
— including when we processed it perfectly and only the acknowledgement got lost. So:

- the same event **will** arrive twice;
- the two copies **will** sometimes arrive at the same instant;
- they **will** sometimes be processed by different machines;
- and events for the same entity **will** arrive out of order.

None of these is a corner case. They are the normal weather. A system that is only correct when they
don't happen is not correct.

---

## The five constraints that do the work

| | enforces | how |
|---|---|---|
| `UNIQUE (source, idempotency_key)` | a redelivery creates no second event | `INSERT ... ON CONFLICT DO NOTHING` |
| `UNIQUE (ledger_entry.event_id)` | an event produces at most one effect, **ever** | same, and it is what makes replay safe for free |
| `FOR UPDATE SKIP LOCKED` | two workers never claim the same **row** | the poll |
| `pg_advisory_xact_lock(hash(entity))` | two workers never process two rows for the same **entity** | taken after the claim |
| `account.version` | a stale event cannot clobber newer state | a strict `>` on the ordering key |

**The third and fourth are not the same thing**, and this is the distinction most implementations
miss. `SKIP LOCKED` protects *rows*. But worker A can take event 1 for account X while worker B takes
event 2 for account X — different rows, so `SKIP LOCKED` is perfectly happy — and then they both read
the same balance and one write lands on top of the other. The thing that needs protecting is the
**account**, not the row. That is what the advisory lock is for.

---

## Request lifecycle

```
provider ──▶ POST /v1/webhooks/{source}
               │
               ├─ verify HMAC + timestamp    ── fail ─▶ 401 (indistinguishable, always)
               ├─ parse                       ── fail ─▶ 400 (only reachable after auth)
               ├─ INSERT ... ON CONFLICT      ─────────▶ the dedup decision, made atomically
               └─ COMMIT ──▶ 200                         ← the row is durable BEFORE we answer
                                                            (NFR-3, ADR-0007)
        ─────────────────────────────────────────────────────────────
                            (async, a different process)

worker ──▶ poll: status='pending' AND next_attempt_at <= now()
             │
             ├─ SELECT ... FOR UPDATE SKIP LOCKED     ← claim one row
             ├─ pg_advisory_xact_lock(hash(entity))   ← serialise on the entity
             ├─ dispatch → handler → Effect           ← pure; no I/O, no session
             ├─ INSERT ledger ON CONFLICT DO NOTHING  ← the effect, at most once
             ├─ UPDATE balance   (same statement's fate as the ledger row)
             ├─ INSERT attempt
             └─ UPDATE status ──▶ COMMIT              ← all of it, or none of it
```

**Everything about one event commits in a single transaction.** That is not tidiness — it is what
makes crash-safety structural. `SIGKILL` a worker at any instruction and Postgres rolls the whole
thing back: the ledger row vanishes together with the balance change that matched it, the event
returns to `pending`, and the advisory lock dies with the connection. There is no reaper, no lease, no
`processing` row to sweep up, and no window in which the system is inconsistent — because there is no
instant at which that state exists. ([ADR-0003](docs/adr/0003-one-transaction-per-event.md))

---

## Layers

Dependencies point inward. The domain never imports the framework.

```
api/        HTTP, signature verification, admin routes    ← FastAPI lives here and nowhere else
services/   ingest, process, replay                       ← orchestration; owns the transactions
domain/     events, effects, handlers, errors, backoff    ← PURE. No session, no clock, no I/O.
adapters/   ORM, queue, ledger, locks, clock, rng         ← the only place that knows SQLAlchemy
worker/     the poll loop
obs/        structlog, prometheus
```

The load-bearing rule is that **a handler is a pure function**: event in, `Effect` out. It issues no
SQL. That keeps the transactional rules — advisory lock held, effect and status committing together —
in exactly one place, instead of being re-implemented, slightly wrong, in every handler somebody adds
later. It also means the interesting half of the system is unit-testable with no database at all.

---

## The decisions worth arguing with

**Postgres as the queue, not a broker.** One datastore means the effect and the queue state commit in
the *same transaction*, which is what makes "no half-applied effect" true without an outbox or a
distributed transaction. The cost is that throughput is bounded by Postgres — and the load test found
we hit the *application* ceiling first, by a factor of five, so that price is not yet being paid.
([ADR-0001](docs/adr/0001-postgres-as-queue.md))

**Ordering binds state-setting effects, not additive ones.** Addition commutes: a late credit is not
stale, it is *late*, and superseding it would silently lose a real payment. Only last-writer-wins
effects can be made wrong by arriving late, so only those consult `account.version`. This is why the
demo domain has a `balance.snapshot` event at all — **an additive-only domain cannot demonstrate
out-of-order handling**, because in an additive-only domain order does not matter.
([ADR-0004](docs/adr/0004-ordering-only-binds-state-setting-effects.md))

**Full jitter, not "backoff ± a bit".** A downstream dies; every event in flight fails within
milliseconds of every other; on a deterministic schedule they all retry at *the same instant* and
knock the recovering downstream straight back over. The mean delay is not what hurts it — the variance
is. ([ADR-0005](docs/adr/0005-full-jitter-and-earned-retries.md))

**Retryability is earned.** SPEC §6.6: what we cannot classify, we do not retry. An unrecognised
exception is far more likely to be our bug than the world's weather, and a bug is not fixed by a
fourth attempt. But that default is only *safe* because the genuinely transient cases are enumerated
by SQLSTATE — `57P01` is an RDS failover, and treating it as "unclassified" would dead-letter every
event in flight over a fifteen-second blip.

**The advisory-lock key is hashed in Python, not in Postgres.** `hashtext()` has no cross-version
stability guarantee. During a rolling upgrade the same account could hash two ways, the workers would
take *different* locks, serialise nothing, and corrupt a balance — silently, with every test still
passing. ([ADR-0002](docs/adr/0002-advisory-lock-key-derivation.md))

---

## What the database enforces that the application cannot

The application is not in the room when someone has a `psql` prompt open at 2am. So:

- **`ledger_entry` is append-only.** The unique constraint stops an effect being applied twice; it
  does nothing about the row *afterwards*. An `UPDATE` or `DELETE` would silently break
  `balance == SUM(ledger)`. Both are now refused — and the `DELETE` guard fires *through* the
  `ON DELETE CASCADE` from `webhook_event`, which is the case that would otherwise let a retention
  sweep quietly destroy a balance.
- **`processing_attempt` cannot be rewritten.** An audit log that can be edited is a rumour.
- **A `resolved` DLQ entry is terminal.** An entry a human ruled on does not quietly reopen.

And `v_account_reconciliation` turns the whole correctness claim into a query: **`drift` must be 0 on
every account, always.** That is what the load test grades against, and it is why the balance is a
*cache* while the ledger is the *truth* — a cache you can check is worth having.

---

## Scaling

**Both tiers are stateless and scale horizontally with no coordination.** No leader, no partition
assignment, nothing shared but the database. `SKIP LOCKED` keeps workers off each other's rows and
the advisory lock keeps them off each other's entities, so `--scale worker=8` needs no other change.

The measured ceiling is the **app process**, not Postgres: at saturation the app container sits at 98%
of one core while Postgres idles at 18% and the workers, having drained 2,466 events in a second, do
nothing at all. Scale the app *tier*, not the process — `uvicorn --workers` would be cheaper, but
`prometheus_client` keeps a per-process registry, so a multi-worker container would under-report its
own metrics and you'd have bought throughput with the instrumentation that tells you whether the
throughput is real. ([`docs/load-test.md`](docs/load-test.md))

The one thing that does *not* scale with workers is a hot entity: events for a single account are
processed strictly one at a time. That is the design working — it is why the balance is right — and it
is the trade NFR-7 accepts explicitly.
