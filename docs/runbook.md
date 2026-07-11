# Runbook

For the person who has just been paged. Symptoms first, causes second.

**The one query that tells you if anything is actually broken:**

```sql
SELECT fn_ledger_invariant_ok();   -- must be true. If it is false, stop and read §5.
```

Everything else is a performance or backlog problem. That one is a correctness problem, and they are
not the same emergency.

---

## 1. "Events are piling up"

```sql
SELECT * FROM v_queue_health;
```

The column that matters is **`due_now`**, not `pending`.

- **`waiting_on_backoff` is high, `due_now` is low** — nothing is wrong with the queue. Those events
  failed and are sitting out a retry delay, exactly as designed. Go to §2: find out *why* they're
  failing.
- **`due_now` is high and climbing** — the workers cannot keep up. This is a capacity problem.

```sql
SELECT fn_queue_lag();   -- how far behind we are, as a duration. THIS is the number to alert on.
```

`fn_queue_lag()` is the right alert, not the pending count: a thousand events processed in a second
is fine, ten events stuck for an hour is not, and only the lag distinguishes them.

**Fix:** add workers. They coordinate through the database — `SKIP LOCKED` keeps them off each
other's rows, the advisory lock keeps them off each other's entities — so there is nothing to
configure:

```bash
docker compose up -d --scale worker=8          # local
aws ecs update-service --desired-count 8 ...   # fargate
```

**If adding workers doesn't help**, they are contending on entities, not on rows. Check whether the
backlog is concentrated on a few accounts:

```sql
SELECT entity_id, count(*) FROM webhook_event
WHERE status = 'pending' GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

Events for *one* entity are processed strictly one at a time (FR-9) — by design, and it is why the
balance is right. Ten thousand events for one account will not go faster with more workers. That is
the design working, not failing.

---

## 2. "Things are failing"

```sql
SELECT event_type, outcome, count(*) FROM v_processing_outcomes GROUP BY 1,2;
SELECT * FROM v_dlq_open;   -- what has given up entirely
```

Then look at one event's history — the attempts tell the story the event row cannot:

```sql
SELECT * FROM v_event_overview WHERE id = :id;
SELECT attempt_number, outcome, error_class, error_detail, duration_ms
FROM processing_attempt WHERE event_id = :id ORDER BY attempt_number;
```

**Three retryable failures with a growing backoff** is a sick downstream. It will heal itself or
exhaust its attempts and dead-letter; you mostly have to decide whether to wait.

**One non-retryable failure** is a bad event or our bug. It is already in the DLQ. Read `error_class`:

| `error_class` | what it means |
|---|---|
| `UnknownEventTypeError` | the provider sent a type we have no handler for. Add the handler, then replay. |
| `UnprocessableEventError` | the payload is missing a field, or has one of the wrong type. Usually a provider change. |
| `TypeError`, `KeyError`, … | **our bug.** SPEC §6.6 sends unclassified exceptions straight to the DLQ rather than retrying them, so this reached a human on the first attempt instead of the fifth. Fix the code, deploy, replay. |

---

## 3. "The DLQ is filling up"

```sql
SELECT reason, count(*) FROM dead_letter_entry
WHERE status = 'needs_review' GROUP BY 1 ORDER BY 2 DESC;
```

The `reason` carries the exception class, so this groups the DLQ by failure mode in one query.

**Once the cause is fixed, replay.** Replay goes through the same claim, the same advisory lock and
the same unique-keyed ledger as normal processing, so replaying an event whose effect already landed
does nothing at all (FR-17, ADR-0008). You cannot double-charge someone by replaying too eagerly.

```bash
curl -H "X-Admin-Key: $ADMIN_API_KEY" -X POST https://.../v1/admin/replay \
     -H 'content-type: application/json' \
     -d '{"dead_lettered": true, "reason": "handler shipped in v1.2"}'
```

Bounded by `REPLAY_MAX_BATCH` (default 100) and drains **oldest first**, so repeating the call makes
progress. The bound is deliberate: replay is synchronous and takes an advisory lock per event, so an
unbounded "replay everything" is a self-inflicted outage wearing a recovery's clothes.

**A replay that fails** puts the entry back to `needs_review` — it is not left stranded in
`replaying`. If it fails again, you have not fixed the cause.

**If the events are junk** (test traffic, a provider's mistake), discard rather than replay:

```bash
curl -H "X-Admin-Key: $ADMIN_API_KEY" -X POST https://.../v1/admin/dlq/{id}/discard \
     -H 'content-type: application/json' -d '{"note": "provider test traffic"}'
```

`resolved` and `discarded` are **terminal** and cannot be undone — the database refuses it, not just
the API (`trg_dlq_terminal_is_terminal`). Be sure.

---

## 4. "Providers are getting 401s"

The 401 is deliberately **indistinguishable** across all four causes — unknown source, missing
header, malformed header, stale timestamp, bad MAC — because telling an attacker *which* one they got
wrong is an oracle. Which means it will tell you nothing either. Read the logs:

```
{"event": "ingest.unauthorized", "failure": "TimestampOutsideToleranceError", "source": "stripe"}
```

| `failure` | cause |
|---|---|
| `UnknownSource` | the `{source}` in the URL is not in `WEBHOOK_SECRETS`. |
| `SignatureMismatchError` | the secret we hold is not the one they signed with. **Check both sides.** |
| `TimestampOutsideToleranceError` | **clock skew**, or a genuinely old replayed request. Check the host's clock before you touch the tolerance. |
| `MalformedSignatureHeaderError` | they are not sending the header we expect. |

> This class of bug has bitten this project twice. Both times the secret in the container and the
> secret used to sign differed, and the correct-by-design 401 told nobody anything. If every request
> from one provider is failing and nothing changed on their side, **compare the secrets before
> anything else.**

---

## 5. "`fn_ledger_invariant_ok()` is false"

Stop. This is the one real emergency: an account's balance no longer agrees with the ledger that is
supposed to prove it.

```sql
SELECT * FROM v_account_reconciliation WHERE drift <> 0;
```

`drift = balance_minor - SUM(ledger_entry.amount_minor)`. There are only three ways it can be
non-zero, and the ledger is append-only (`trg_ledger_entry_immutable`), so:

1. **Somebody edited `account.balance_minor` by hand.** By far the most likely. The ledger is the
   truth; the balance is a cache. Fix the cache:
   ```sql
   UPDATE account a SET balance_minor = fn_account_balance(a.external_ref) WHERE a.id = :id;
   ```
2. **A ledger row was deleted with the escape hatch** (`SET LOCAL app.allow_ledger_delete = 'on'`).
   Check who, and why. There is no legitimate reason outside archival.
3. **A genuine double-application** — which should be impossible, because the balance can only move
   on the same statement that claims the unique ledger row (ADR-0008). If it is this, the design is
   broken and you have found something that matters. Do not "fix" the balance until you know which of
   the three it was: correcting the number destroys the evidence.

---

## 6. Deploys

**Migrations run as a separate one-shot task, to completion, before the services update.** Never as
an app startup step: two tasks racing `alembic upgrade head` is a real way to corrupt a schema.

```bash
alembic upgrade head --sql    # review the SQL before it touches production
alembic downgrade -1          # every migration in this repo has a working downgrade
```

**Rollback** is the previous image tag. Tags are immutable (`IMMUTABLE` on the ECR repo), so
`v0.1.3` means one thing forever. If a migration is involved, roll the code back first and only then
decide about the schema — a schema downgrade that drops a column takes the data with it.

**A worker killed mid-event loses nothing.** Everything about one event — the claim, the ledger row,
the balance, the attempt record, the status — commits in a single transaction (ADR-0003). `SIGKILL`
it at any instant and Postgres rolls the lot back; the event returns to `pending` and the next poll
picks it up. There is no reaper and nothing to clean up.

---

## 7. Numbers worth alerting on

| metric | why |
|---|---|
| `fn_queue_lag()` > 5 min | the provider would notice. This is the real SLO. |
| `webhook_events_dead_lettered_total` rate > 0 | something is systematically broken. |
| `fn_ledger_invariant_ok()` = false | **page immediately.** See §5. |
| `webhook_ingest_duration_seconds` p99 > 50 ms | the app tier is saturating. Scale it out — CPU is the bound, not the database (`docs/load-test.md`). |
| `webhook_events_rejected_total{reason="unauthorized"}` spike | either an attack, or a secret rotation somebody forgot to tell you about. |

Note that ingestion and processing metrics come from **different processes**. The app serves
`/metrics` on `:8000`; each worker serves its own on `:9100`. Prometheus scrapes a process, not an
application — scrape both, or the half of the pipeline where events actually fail will be invisible.
