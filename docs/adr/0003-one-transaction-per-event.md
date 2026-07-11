---
status: accepted
date: 2026-07-11
deciders: backend
consulted: SPEC.md §FR-7, §NFR-3, §NFR-4
---

# ADR-0003: One transaction per event, with no separate claim phase

## Context and Problem Statement

A worker has to take an event, do the work, and record that it did — without two workers taking the
same event, and without a crash halfway through leaving the system in a state nobody can reason
about.

The conventional shape is **claim-then-process**: one transaction flips a batch of rows to
`processing` and commits, and the work then happens outside it. It is the shape most job-queue
tutorials show, and it is what the `webhook_status.processing` enum value in SPEC §3 anticipates.

It has a failure mode. If the worker dies after the claim commits and before the work finishes,
those rows are `processing` forever. No other worker will touch them, because the poll predicate
only selects `pending`. The events are not lost — they are *stranded*, which is worse, because
nothing is on fire and nothing is retried. Recovering them needs a **reaper**: a second background
process that reclaims rows that have been `processing` for suspiciously long. And "suspiciously
long" is a guess. Set it too short and the reaper steals rows from workers that are merely slow,
processing them twice. Set it too long and a crashed worker's events sit for an hour.

## Considered Options

1. **One transaction per event**: claim, lock, apply, record, and finish — all inside a single
   transaction, committed or rolled back as a unit.
2. **Claim-then-process** with a `processing` status and a stale-claim reaper.
3. **Claim-then-process** with a lease column (`locked_by`, `locked_until`) and lease renewal.

## Decision Outcome

Chosen: **option 1**.

The claim is `SELECT ... FOR UPDATE SKIP LOCKED` on a single row, and the transaction it opens stays
open until the event is finished. The row lock *is* the claim, so there is nothing to release and
nothing to sweep up.

Crash-safety then stops being a feature we implement and becomes a property we cannot avoid: kill
the worker with `SIGKILL` at any instruction, pull its network cable, OOM it mid-handler, and
Postgres rolls the transaction back. The ledger row disappears together with the balance update that
matched it (NFR-4: no half-applied effect). The event returns to `pending` and the next poll picks it
up (NFR-3: nothing acknowledged is lost). The advisory lock is released when the connection dies. No
reaper, no lease, no timeout to tune, and no window in which the system is inconsistent — because
there is no instant at which the inconsistent state exists.

It also makes the interesting invariant free. The **effect** (`ledger_entry` + `account.balance`) and
the **queue state** (`webhook_event.status`, `attempt_count`) are written in the same transaction, so
they cannot disagree. An event marked `succeeded` with no ledger row, or a ledger row against an
event still marked `pending`, are not states this design can produce. That is the payoff ADR-0001
promised when it chose one datastore over a broker plus a database.

### Consequences

* Good: crash-safety is structural. There is no recovery path to write, and therefore none to get
  wrong.
* Good: no reaper, no lease timeout, no `locked_until` — three tunables that do not exist cannot be
  misconfigured.
* Good: a failure in one event cannot roll back its neighbours in the batch, because they were never
  in the same transaction.
* Bad: **`webhook_status.processing` is now written by nothing.** The row lock replaces it, and a
  transient status that is only visible inside its own uncommitted transaction is not visible at all.
  The value is retained rather than migrated away: removing a Postgres enum value is a
  `CREATE TYPE`/swap/`DROP TYPE` dance, and the Day 3 admin API may yet want it to mark an event as
  in-flight for an operator.
* Bad: the ORM comment claiming `processing_attempt.finished_at IS NULL` is "the signature of a
  worker that died mid-processing" is no longer true, and has been corrected. Under this design an
  attempt row only becomes visible once it is complete, so a half-written attempt cannot be observed.
  We trade that diagnostic for never having produced the half-written state in the first place.
* Bad: **a handler holds a database transaction for its whole duration.** For this service that is
  fine — the handler's work *is* a database write. It would stop being fine the moment a handler made
  a slow external HTTP call, because the transaction (and its pooled connection) would be held across
  the network round-trip, and a downstream slowdown would turn into connection-pool exhaustion. If a
  handler ever needs to call out, this decision must be revisited: that is the point at which
  claim-then-process and a reaper start earning their complexity.

### The failure path is the exception

One thing this decision does *not* buy: failure bookkeeping cannot happen in the transaction that
failed. Once a database statement errors, Postgres aborts the transaction and refuses every
subsequent statement in it — including the `INSERT` recording what went wrong. So the failure path
opens a **second, fresh transaction** after the first has rolled back, which is why
`process_event()` takes a session factory rather than a session. This is not incidental; it is the
one wrinkle in an otherwise uniform design, and it is worth knowing about before reading the code.
