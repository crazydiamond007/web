# Load test: the number

> **20,910 duplicate deliveries. 2,466 distinct events. 2,466 ledger rows. Zero double-processing.
> Ingestion p99 of 26 ms, sustained 350 deliveries/s.**

Reproduce with `make load`. The raw output is in [`tests/load/results/`](../tests/load/results/).

---

## What is actually being tested

Not throughput. Throughput is a by-product. The test exists to answer one question under conditions
designed to break the answer:

```
count(ledger_entry) == count(distinct events)
```

Three deliberate choices make that question hard to pass by accident:

**Every event is delivered twice, back to back.** That is what an at-least-once provider does when our
`200` does not reach it in time. So ~88% of the traffic here is a redelivery of something already
stored — the exact condition the whole service exists to survive.

**Only 50 accounts.** Spreading 20,000 events over 20,000 accounts would prove nothing: nothing would
ever contend, `pg_advisory_xact_lock` would never be taken under pressure, and the test would pass on
a system with no locking at all. Concentrating them forces the workers to actually collide.

**Four workers, racing.** `FOR UPDATE SKIP LOCKED` has to arbitrate for real.

And the verdict is read **from the database by a separate process** (`scripts/verify_load.py`), not
reported by the load generator. A load test that grades its own homework is not evidence.

## The result

```
20,603 deliveries sent -> 2,466 events stored (18,137 duplicates absorbed, 88%)

  [PASS] every stored event is unique                      2466 rows, 2466 distinct (source, idempotency_key)
  [PASS] nothing was dead-lettered                         0 dead-lettered
  [PASS] every event was processed                         2466 of 2466 succeeded
  [PASS] count(ledger_entry) == count(distinct events)     2466 ledger rows, 2466 distinct events
  [PASS] balance == SUM(ledger) on every account           50 accounts, 0 with drift
  [PASS] balance == amount x distinct events, per account  every balance is exactly what arithmetic predicts
```

The last three are not the same test, and a broken system could pass any one of them alone. The
fourth counts rows. The fifth checks the cached balance agrees with those rows. The sixth pins the
**absolute number** — an account's balance must be exactly `100 × (its distinct events)` — which is
the only one that would catch a double-application *and* a compensating bug that hid it.

## Latency

| | first delivery | redelivery |
|---|---|---|
| p50 | 9 ms | 9 ms |
| p95 | 19 ms | 19 ms |
| **p99** | **27 ms** | **26 ms** |
| max | 53 ms | 59 ms |

**A redelivery costs the same as a first delivery.** That is not a footnote. If the duplicate path
were slower, then a provider hammering us with retries *during* an incident would make the incident
worse — the failure mode would be self-amplifying. It is the same speed because deduplication is a
single `INSERT ... ON CONFLICT DO NOTHING`, not a lookup followed by a decision.

NFR-2's budget is p99 < 50 ms. **26 ms, met.**

## Where the ceiling is

Latency is a function of concurrency, so a single p99 number means nothing without the load it was
measured at. The sweep ([`concurrency_sweep.csv`](../tests/load/results/concurrency_sweep.csv)):

| concurrent clients | throughput | p50 | p95 | p99 |
|---|---|---|---|---|
| 4 | 220 /s | 5 ms | 9 ms | 11 ms |
| **8** | **362 /s** | **8 ms** | **16 ms** | **21 ms** ✅ |
| 16 | 389 /s | 26 ms | 49 ms | 79 ms ❌ |
| 32 | 396 /s | 66 ms | 95 ms | 120 ms ❌ |
| 50 | 355 /s | 110 ms | 260 ms | 370 ms ❌ |

Read it as: **throughput plateaus at ~390/s while latency keeps climbing.** That is the signature of a
saturated resource, not of a system doing more work. Past ~8 concurrent clients we are not going
faster, we are only queueing.

`docker stats` during a saturated run names the culprit outright:

```
webhook-receiver-app-1        98.4%   <- pegged, one core
webhook-receiver-postgres-1   18.6%
webhook-receiver-worker-1..4   0.2%   <- idle; they drained 2,466 events in 1s
```

**The bottleneck is the single app process, not the database.** It is CPU-bound on HMAC verification,
JSON parsing, and one round-trip, all in one single-threaded event loop. Postgres has ~80% headroom;
the worker tier is asleep.

This is worth stating plainly because ADR-0001 accepted "throughput bounded by Postgres" as the price
of using it as the queue. **At this scale that price is not being paid** — we hit the application
ceiling first, by a factor of five. The trade-off ADR-0001 worried about is real, but it is not yet
the binding constraint.

## So how do you make it faster?

**More app containers, not more processes per container.**

Uvicorn will happily run `--workers 4` and roughly quadruple this number. It is deliberately not done,
because `prometheus_client` keeps its registry **per process**: a four-worker container would serve
`/metrics` from whichever worker happened to answer the scrape and silently under-report by ~4×. You
would have bought throughput with the instrumentation that tells you whether the throughput is real.

One process per container keeps each container a clean scrape target — which is exactly the shape
Fargate and Kubernetes want anyway. The app tier is stateless (NFR-4), so it scales horizontally
behind a load balancer with no coordination at all.

The worker tier already demonstrates this: four of them, no leader, no partitioning, nothing shared
but the database, and they drained the entire backlog in one second.

## Caveats, stated rather than buried

- **One laptop.** The load generator, four workers, the app, and Postgres all share the same CPU
  under WSL2. The absolute numbers would be higher on separate hosts — but the *correctness* result
  is unaffected by that, and the correctness result is the point.
- **The ~390/s ceiling is this machine's**, not the design's. It measures one uvicorn process on one
  contended core.
- **`p99 = 26 ms` is at 8 concurrent clients.** Quoting a p99 without the concurrency it was measured
  at is meaningless, so it is quoted with it, and the whole curve is in the table above.
