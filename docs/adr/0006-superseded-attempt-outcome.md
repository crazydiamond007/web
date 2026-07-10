---
status: accepted
date: 2026-07-10
deciders: backend
consulted: SPEC.md §3, FR-10
---

# ADR-0006: Add `superseded` to the `attempt_outcome` enum

## Context and Problem Statement

`SPEC.md` §3 fixes `attempt_outcome` as `succeeded | retryable_error | non_retryable_error`.

FR-10 requires that when a stale event arrives after a newer one has already moved the entity
forward, "the stale event is recorded as **superseded** rather than applied."

None of the three permitted outcomes can express that. The attempt did not fail, so neither error
value is honest. And it did not apply an effect, so `succeeded` would make the attempt log claim a
`ledger_entry` exists when it does not — the two tables an operator cross-references during an
incident would disagree.

This is a genuine gap in the spec, not an implementation preference.

## Considered Options

1. **Add `superseded` to `attempt_outcome`.**
2. Record the attempt as `succeeded` and stash `"superseded"` in `error_class`.
3. Record it as `non_retryable_error` with a reason of `"superseded"`.
4. Add a `superseded` value to `webhook_status` instead.

## Decision Outcome

Chosen: **option 1**, add the enum value.

The outcome column answers "what happened on this pass?". A fourth answer exists in the domain, so
a fourth value belongs in the enum. The alternatives all encode a domain fact as a magic string or
a lie:

* **Option 2** puts a control-flow value in a column named `error_class`, so every query that
  counts successes has to remember to exclude a particular string. Whoever forgets gets a wrong
  number, silently.
* **Option 3** would send superseded events to the dead-letter queue, since the retry policy
  dead-letters non-retryable failures immediately (FR-11). Arriving late is not a failure and must
  not page anyone.
* **Option 4** conflates the event's lifecycle with a single attempt's result. The event *is*
  terminally handled; `webhook_status = succeeded` is correct for it. It is the attempt that needs
  the nuance.

### Consequences

* Good: the invariant `count(ledger_entry) == count(attempt WHERE outcome = 'succeeded')` holds
  exactly, which is what lets the load test assert correctness rather than assume it.
* Good: `superseded` is directly queryable, so out-of-order delivery becomes an observable rate
  rather than an inference.
* Bad: a deliberate, documented deviation from `SPEC.md` §3. Flagged to the spec owner.
* Bad: NFR-1's headline check, `count(ledger_entry) == count(distinct events)`, only holds over
  events that are neither superseded nor dead-lettered. The Day 4 load test replays additive,
  non-poison events, where it does hold.

### Why the initial migration, not a later one

Postgres cannot run `ALTER TYPE ... ADD VALUE` inside a transaction block on versions before 12,
and even on 16 the new value is unusable in the same transaction that adds it. Alembic wraps each
migration in a transaction, so retrofitting an enum value means either a non-transactional
migration or the `CREATE TYPE ... new` / swap / `DROP TYPE` dance. Introducing the value in
`0001_initial_schema` costs nothing now and avoids that entirely.

Reverting is a one-line change to `0001` while the schema is still unreleased.
