---
status: accepted
date: 2026-07-11
deciders: backend
consulted: SPEC.md §FR-1, §FR-2, §NFR-2, §NFR-3
---

# ADR-0007: Accept and queue, never process inline

## Context and Problem Statement

A webhook arrives. We can either **do the work and then answer**, or **store it, answer, and do the
work afterwards**.

Processing inline is simpler in every visible way: one code path, no worker, no queue table, no
"pending" state, and the caller learns immediately whether their event succeeded. It is what almost
every first implementation does.

It is also how you lose events.

## Decision Outcome

**The row is committed, and only then is `200` returned. Nothing else happens on the request path.**

The reasoning is about what a provider does when we are slow, and it is worth spelling out because it
inverts the usual intuition that "slower is just slower".

A provider's delivery has a timeout — Stripe's is around 20 seconds, and several are far shorter.
If we process inline and the work takes longer than that, the provider gives up waiting and
**redelivers**. Our first attempt is still running. Now two copies of the same event are being
processed concurrently, and if the downstream is slow enough to have caused this, it is slow enough
for the retries to pile up behind each other. Load rises, latency rises, more deliveries time out,
more get redelivered. The system's response to being slow is to generate more work for itself.

Accept-and-queue makes the acknowledgement time **independent of the work**. Ingestion is an insert:
its p99 is 26 ms and it stays 26 ms whether the handler is instant or takes a minute (see
`docs/load-test.md`). A slow downstream now makes the *queue* longer, which is a thing you can see on
a dashboard and scale workers at — instead of making the *provider* angry, which is a thing that
makes it hit you harder.

**The order matters absolutely: commit, then answer.** A `200` is a promise that the event will be
processed. Answering before the commit — even microseconds before — means a crash in that window
loses an event we have already promised to handle, and the provider will never send it again, because
as far as it is concerned we said yes. This is NFR-3, and it is why the transaction boundary is
*inside* the handler and not deferred to a background task.

### What this costs

* **The caller learns nothing about whether the event succeeded.** `200` means "durably accepted",
  not "applied". A provider does not care — it has no way to act on the difference — but it means an
  operator needs somewhere else to look, which is what the admin API (FR-18) and the DLQ (FR-14) are
  for.
* **A queue table exists**, with the poll, the claim, the status column, and the index behind them.
  That is real complexity that inline processing does not have.
* **Events are processed with a delay** bounded by the poll interval. For a webhook this is
  irrelevant — the provider has already gone — but it would not be for a synchronous API.

### The alternatives

* **Process inline.** Rejected above. It is not just slower under load; it is *unstable* under load,
  because the failure mode feeds itself.
* **Process inline with a short timeout, and queue on timeout.** Two code paths for one operation,
  and the interesting one — the timeout path — is the one that never runs in testing and always runs
  during an incident.
* **Answer `202 Accepted` instead of `200`.** More honest, and wrong in practice: several providers
  treat anything other than `2xx`... which `202` is. It would work. It is not worth the risk that a
  provider's retry logic special-cases `200`, for a semantic nicety no machine reads.

### Consequences

* Good: **ingestion latency is decoupled from processing latency.** A downstream outage cannot
  produce a provider-side retry storm, because we never make the provider wait for the downstream.
* Good: it is what makes the rest of the design possible. The worker can take an advisory lock, retry
  with backoff, and take seconds over an event, precisely because nobody is holding a socket open
  waiting for it.
* Good: NFR-3 becomes a single reviewable line — the `session_scope` exits before the `JSONResponse`
  is constructed.
* Bad: "we returned 200" and "it worked" are different statements, and every operator has to learn
  that.
