---
status: accepted
date: 2026-07-11
deciders: backend
consulted: SPEC.md §FR-5, §FR-6, §NFR-1
---

# ADR-0008: Idempotency is a constraint, not a check

## Context and Problem Statement

The service must apply each event exactly once, under redelivery, under replay, and under two workers
racing on the same account. There are two places this has to hold:

* **at the door** — a redelivered webhook must not become a second event row (FR-5);
* **at the effect** — a reprocessed event must not become a second ledger row (FR-6).

The obvious implementation of both is a check:

```python
if not await already_exists(key):      # SELECT
    await insert(...)                  # INSERT
```

## Decision Outcome

**Neither is implemented as a check. Both are `INSERT ... ON CONFLICT DO NOTHING` against a `UNIQUE`
constraint**, and the application learns what happened from whether `RETURNING` came back empty.

```sql
INSERT INTO webhook_event (...) VALUES (...)
ON CONFLICT ON CONSTRAINT uq_webhook_event_source_idempotency_key DO NOTHING
RETURNING id;                                    -- empty => it was a duplicate
```

### Why the check is wrong

`SELECT`-then-`INSERT` is a **check-then-act race**, and it fails under exactly the conditions it was
written to handle.

Two redeliveries of the same event arrive at the same instant — which is not a corner case, it is
what a provider *does* when it thinks we timed out. Both run the `SELECT`. Both find nothing. Both
insert. The check was never atomic: there is a window between the read and the write, and under
concurrency something else fits inside it.

The cruel part is that it *passes every test written against it*. Sequential tests never open the
window. It survives code review, because the code says what the author meant. It ships. And then it
fails in production, intermittently, under load, on the one path where money moves — and the evidence
is a wrong number, discovered days later, by someone else.

`ON CONFLICT` has no window. The uniqueness decision is made inside the database, atomically, under
whatever concurrency you throw at it, across processes and across machines. There is nothing to get
right, so there is nothing to get wrong.

### Why `RETURNING` rather than a follow-up `SELECT`

An empty `RETURNING` *is* the answer: it means our insert lost. Asking a second question ("does it
exist now?") would reintroduce a read whose answer could already be stale. We do read the existing
id afterwards, to put in the response — but that read is not part of the dedup decision, which was
already made and committed.

### Why the ledger constraint is on `event_id`

`uq_ledger_entry_event_id` is the whole of FR-6, and it is deliberately keyed on the *event*, not on
some hash of the effect's contents. That means:

* replaying an event is safe **for free** — the replay path (FR-16) runs into the same constraint,
  and the second insert simply does nothing. Replay contains no idempotency logic of its own, which
  matters because a second implementation of exactly-once is the one with the bug (ADR see
  `services/replay.py`);
* the balance can only move on the **same statement** that successfully claims the ledger row, so
  "effect applied" and "effect recorded" cannot drift apart;
* the invariant `balance == SUM(ledger_entry.amount_minor)` holds by construction, which makes
  correctness *checkable with a query* rather than assertable in prose. That is
  `v_account_reconciliation`, and it is what the load test grades against.

### Consequences

* Good: **correctness does not depend on the application being right.** Delete the service, write a
  new one in another language, point it at this schema, and it still cannot double-process. The
  guarantee lives in the schema.
* Good: the load test can state its result as a query — `count(ledger_entry) == count(distinct
  events)`, 2,466 = 2,466 — rather than as a claim.
* Good: the dedup path is *as fast as* the happy path (p99 26 ms vs 27 ms in the load test), because
  it is one statement, not a lookup followed by a decision. A slower duplicate path would be actively
  dangerous: it is the path a provider hammers hardest during an incident.
* Bad: the constraint is named in application code (`on_conflict_do_nothing(constraint=...)`), so
  renaming it in a migration breaks the insert. That is on purpose — the alternative,
  `on_conflict(columns=...)`, would silently start inserting duplicates if the constraint were ever
  dropped, and a loud failure beats a quiet one.
* Bad: `ON CONFLICT DO NOTHING` still consumes a sequence value on the failed insert, so
  `webhook_event.id` has gaps. Nobody cares, and it is worth knowing before somebody "fixes" it.
