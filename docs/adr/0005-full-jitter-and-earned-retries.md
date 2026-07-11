---
status: accepted
date: 2026-07-11
deciders: backend
consulted: SPEC.md §FR-11, §FR-12, §FR-13, §6.4, §6.6
---

# ADR-0005: Full jitter, and retries that have to be earned

## Context and Problem Statement

Two decisions, taken together because getting either one wrong produces the same outcome — a retry
policy that makes an incident worse than the fault that started it.

1. **How long do we wait before retrying?**
2. **What do we retry at all?**

## Decision 1: full jitter

`delay = random(0, min(cap, base * 2**attempt))`.

Exponential-with-cap is uncontroversial. The interesting choice is the **jitter**, and the options
are not equivalent:

| | delay after attempt 3 (base 1s) |
|---|---|
| none | exactly 8s |
| "equal jitter" (`d/2 + random(0, d/2)`) | 4s–8s |
| **full jitter** (`random(0, d)`) | **0s–8s** |

Chosen: **full jitter**.

The failure this prevents is *synchronisation*, and it is worth spelling out because the mechanism is
not obvious. A downstream goes down for thirty seconds. Every event in flight fails within a few
milliseconds of every other. With a deterministic schedule they all then wait *exactly* the same
number of seconds — so they all retry at *exactly* the same instant. The downstream, which has just
come back up, takes the entire backlog in one burst, falls over again, and the fleet re-synchronises
harder on the next round, because now they failed even closer together. The retry policy has become a
self-inflicted DDoS with a metronome, and adding workers makes it worse.

Full jitter is counter-intuitive: the *expected* delay is halved, so on average we retry **sooner**,
which feels like the wrong direction when a downstream is struggling. But the mean is not what is
hurting the downstream — the **variance** is. Spreading the retries uniformly over `[0, ceiling)`
turns a spike into a trickle. AWS measured this ("Exponential Backoff and Jitter", 2015): full jitter
beat both no-jitter and equal-jitter on total completion time *and* on load against the server.
Equal jitter keeps a floor, and that floor is exactly the clustering we are trying to destroy.

The RNG is injected and seedable (`JITTER_SEED`), so the schedule is assertable rather than merely
plausible (SPEC §6.4). `SystemRng` holds its **own** `random.Random` instance rather than using the
module-level one, because a process-global RNG means a test that seeds it silently changes the
behaviour of every other test in the same process, in an order-dependent way.

**Seeding in production would be a correctness bug**, not a debugging convenience: every worker would
draw the same sequence, and the fleet would re-synchronise — the precise failure jitter exists to
prevent. The worker logs a warning at `WARNING` if it starts with a seed set.

## Decision 2: retryability is earned, not assumed

SPEC §6.6: *"anything unclassified is treated as non-retryable and dead-lettered."*

**Day 2 shipped the opposite**, and this ADR is also the record of fixing it. `_record_failure` read
`retryable = not isinstance(exc, NonRetryableError)`, so anything we did not recognise — a `TypeError`
in a handler, a `KeyError` on a payload field — was retried up to `MAX_ATTEMPTS`. That is the wrong
default: an exception we cannot classify is far more likely to be **our bug** than the world's
weather, and a bug is not fixed by a fourth attempt. It wastes the retry budget, delays every event
queued behind it, and buries the stack trace under four identical copies of itself.

So the default is now `False`. Retryability is something a failure earns:

1. the **domain taxonomy wins first** — a handler saying "this is permanent" knows more about its
   event than any driver heuristic can;
2. then **transient infrastructure is recognised explicitly**, by Postgres SQLSTATE;
3. everything else is non-retryable and goes to a human.

### The default is only safe because step 2 exists

This is the part that makes the rule work, and skipping it would be a disaster. `57P01`
(`admin_shutdown`) is what an RDS failover looks like from the client. If that fell through to the
"unclassified" default, a **fifteen-second blip would dead-letter every event in flight** and demand
a manual replay of thousands of them. The same goes for `40001` (serialisation failure), `40P01`
(deadlock — Postgres shot one of two transactions and the survivor committed), `53300` (connection
saturation), and the `08xxx` connection family. None of these is a fact about the event; all of them
are cured by trying again.

They are matched on **SQLSTATE**, not on the exception class or the message text. The code is part of
the Postgres wire protocol, so it survives a driver upgrade and a server whose locale translates the
message.

### Consequences

* Good: a poison event costs **one** attempt, not `MAX_ATTEMPTS`, and reaches a human immediately
  (FR-11's acceptance, exactly as written).
* Good: a database failover costs a retry, not a backlog.
* Good: the schedule is deterministic under a seed, so `test_the_ceiling_grows_with_each_attempt`
  asserts the *envelope* (2s, 4s, 8s) rather than hoping a sample lands somewhere plausible.
* Bad: a genuinely transient failure that we have **not** enumerated is now dead-lettered on its first
  attempt, where before it would have been retried. This is the cost of the §6.6 default and it is
  the right side to err on — a dead-lettered event is visible, recoverable with one replay call, and
  loses nothing; a silently retried bug is none of those. The mitigation is that `RETRYABLE_SQLSTATES`
  is a list we extend as we learn, and every dead-letter records the exception class that caused it,
  so the gaps announce themselves.
* Bad: `is_retryable` is an adapter that knows about SQLAlchemy *and* about the domain taxonomy. That
  is the price of an anti-corruption layer; the alternative is `sqlalchemy.exc` imports in the domain,
  which is worse.
