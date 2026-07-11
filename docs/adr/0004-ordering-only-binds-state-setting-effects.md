---
status: accepted
date: 2026-07-11
deciders: backend
consulted: SPEC.md §FR-10, §3
---

# ADR-0004: The ordering guard binds state-setting effects, not additive ones

## Context and Problem Statement

FR-10 requires that a late event must not clobber newer state, and that a stale event be "recorded as
superseded rather than applied". SPEC §3 gives us `account.version` to do it with.

The obvious reading is: *every* event carries an ordering key, and any event whose key is not newer
than what the account has already seen is superseded. Apply the guard uniformly and the requirement
is met.

That reading is wrong, and applying it would be a defect that silently loses money.

The demo domain's primary effect is a **credit**: `balance = balance + amount`. Addition is
commutative. A credit of 500 followed by a credit of 300 lands on exactly the same balance as 300
followed by 500. A credit that arrives "late" is not stale — it is simply late, and it still has to
be applied. Superseding it because a newer event got there first would silently drop a real payment.

The inverse is equally true and equally sharp. An **absolute** effect — "the balance is now 1000" —
is last-writer-wins. Applying an older one after a newer one rewinds the account to a value that
stopped being true, with no error and no trace.

So ordering is not a property of the *event*. It is a property of the *effect the event produces*.

There is a second problem hiding behind the first: **an additive-only domain cannot demonstrate FR-10
at all.** If every effect commutes, order never matters, and any test claiming to prove out-of-order
handling proves nothing. The demo domain as drafted had no event type that could go stale.

## Considered Options

1. **Guard only state-setting effects.** Add a `balance.snapshot` event type (an absolute balance the
   provider reconciles us to). `Credit` always applies; `SetBalance` is guarded by `account.version`.
2. **Guard every event uniformly** by `provider_sequence`.
3. **Guard nothing**, and argue that an additive ledger does not need ordering.
4. **Reject out-of-order events at ingestion**, so the worker never sees one.

## Decision Outcome

Chosen: **option 1**.

`account.version` holds the `provider_sequence` of the newest event applied to the account — a
**high-water mark**, not a row counter. `SetBalance` consults it and supersedes itself if its own
sequence is not strictly greater. `Credit` never supersedes; it applies unconditionally, and it
advances the mark with `GREATEST(version, sequence)` so it can raise the bar but never lower it.

The distinction lives in the type of the effect, not in a flag on the handler, so it is impossible to
add a new state-setting effect and forget to think about ordering — the adapter matches on the effect
and the guard comes with it.

A `SetBalance` with no `provider_sequence` is refused as non-retryable rather than applied
unordered. A last-writer-wins effect with no way to tell who wrote last cannot be applied safely, and
the conservative failure (dead-letter it; a human looks) is much cheaper than the optimistic one (a
stale snapshot rewinds a live account).

The ledger row for an applied snapshot records the **delta** it represents, not the absolute balance,
so `balance == SUM(ledger_entry.amount_minor)` continues to hold. That invariant is what lets the Day
4 load test prove NFR-1 with a `COUNT` and a `SUM` rather than by trusting the application.

### Rejected

* **Option 2 (guard everything)** is the bug described above: it discards late credits. It would even
  pass a naive test suite, because a test that delivers events in order never exercises the branch
  that throws money away.
* **Option 3 (guard nothing)** is defensible *only* for a purely additive domain — and it would make
  FR-10 undemonstrable, which is the same as making it unproven. It also breaks the moment anyone
  adds a state-setting event type, and they would have no reason to think it might.
* **Option 4 (reject at ingestion)** contradicts FR-1 and NFR-3. Out-of-order delivery is *normal* for
  a webhook provider; refusing the event means the provider retries it, and we refuse it again. And
  we cannot even tell at ingestion time: whether an event is stale depends on what has been
  *applied*, which the ingestion path deliberately does not know.

### Consequences

* Good: FR-10 is provable. `test_a_stale_snapshot_is_superseded_not_applied` delivers sequence 2 then
  sequence 1 and asserts the balance stays at the newer value, the stale event writes no ledger row,
  and its attempt is recorded as `superseded`.
* Good: the failure mode that would have been introduced by the naive reading — a dropped credit — is
  itself covered by a test (`test_a_credit_is_never_superseded`).
* Bad: the demo domain grew a third event type it did not strictly need for FR-6. This is the cost of
  being able to demonstrate FR-10 instead of asserting it, and it is worth paying.
* Bad: `account.version` had to be widened from `integer` to `bigint` (migration 0002), because it
  now stores a provider sequence and `webhook_event.provider_sequence` is `bigint`. As `integer` it
  would truncate above 2^31-1 and start comparing against a wrong number — discarding live snapshots
  as stale.
* Bad: a snapshot sequence of `0` is not usable, since `0` is the `version` of an untouched account
  and the guard is a strict `>`. Handlers reject it explicitly rather than accepting a snapshot that
  could never win.
