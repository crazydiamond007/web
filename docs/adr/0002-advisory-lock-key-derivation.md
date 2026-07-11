---
status: accepted
date: 2026-07-11
deciders: backend
consulted: SPEC.md §FR-9, §4
---

# ADR-0002: Derive the advisory-lock key in Python, not in Postgres

## Context and Problem Statement

FR-9 serialises processing per business entity with `pg_advisory_xact_lock(hash(entity))`. Postgres
advisory locks are keyed by a **signed 64-bit integer**, but an entity is a `(entity_type,
entity_id)` pair of `text`. Something has to turn one into the other, and the correctness of FR-9
rests entirely on that mapping being *the same function everywhere*.

If two workers derive different keys for the same account, they take different locks, serialise
nothing, and interleave their read-modify-write on the balance. Nothing raises. Nothing logs. The
only evidence is a wrong number, discovered later, by someone else.

## Considered Options

1. **Hash in Python** with `blake2b`, truncated to 8 bytes, read as a signed int.
2. **Hash in Postgres** with `hashtext($1)` — the obvious choice, and what most examples show.
3. **Two-key form**, `pg_advisory_xact_lock(classid, objid)`, with a per-entity-type `classid` and
   a hash of the id as `objid` (two 32-bit ints).
4. **A lock table** — a real row per entity, taken with `SELECT ... FOR UPDATE`.

## Decision Outcome

Chosen: **option 1**, `blake2b(entity_type + NUL + entity_id, digest_size=8)`, read big-endian and
signed.

**`hashtext` is not part of Postgres' contract.** It is an internal function; its output is not
documented as stable across major versions, and the hash functions behind it have been changed
before. That is normally harmless — nobody persists a `hashtext` value — but here the value *is* the
lock, and the window that matters is a **rolling upgrade**: for the minutes during which some
workers talk to a PG 16 primary and others to a PG 17 one, the same account can hash two ways. This
is the worst class of bug we could ship: silent, intermittent, load-dependent, and invisible to
every test in the suite, because a single-version test environment can never reproduce it.

Hashing in Python moves the function into something we version, pin, and lock: `blake2b` is in the
standard library, its output is fixed by the algorithm, and `tests/unit/test_locks.py` pins the key
for a known entity to a **literal integer**. If anyone swaps the hash, that test fails loudly on the
next commit rather than quietly on the next deploy.

It also makes the key derivable without a database round-trip, so it can be logged, asserted on, and
reasoned about in a unit test.

### Rejected

* **Option 3 (`classid`, `objid`)** is a real improvement on collisions — the entity type gets its
  own 32-bit namespace, so unrelated types cannot collide at all. But `objid` is then only 32 bits,
  and a 32-bit space has a ~50% collision probability at about 77,000 entities (birthday bound).
  Trading a 2^64 space for a 2^32 one to gain type separation is a bad trade when the 2^64 space
  already includes the type in the hashed input.
* **Option 4 (a lock table)** is genuinely correct and has no collisions at all. It costs a row per
  entity, an insert on first sight of every new account, index maintenance, and vacuum pressure, and
  the rows outlive the locks. Advisory locks are the feature Postgres provides precisely so that we
  do not have to build this.

### Consequences

* Good: the lock key is deterministic, version-independent, and pinned by a test.
* Good: `advisory_lock_key()` is a pure function — no session, no I/O — so the interesting property
  (stability) is asserted in a unit test rather than inferred from an integration one.
* Bad: **hash collisions are possible.** Two unrelated entities can map to one key and serialise
  against each other. At 2^64 keys this is negligible, and the consequence is bounded: the system is
  *slower*, never *wrong*. The inverse trade — a scheme that never blocks unnecessarily but can miss
  a real conflict — would be unacceptable, so this is the direction to err in.
* Bad: the key is opaque in `pg_locks`. An operator looking at a blocked lock sees an integer, not
  an account. Mitigated by logging `entity_type`, `entity_id`, **and** the derived key together on
  contention, so the two can be joined from the logs.
