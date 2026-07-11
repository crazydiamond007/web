# Database objects

Everything migration `0003` adds, with a query you can paste into DataGrip to exercise it.

Connect with `make db-url` (host `localhost`, port from your `.env` — **5433** on this machine,
database `webhook_receiver`, user/password `webhook`/`webhook`).

To list them all from a SQL client:

```sql
SELECT table_name AS view      FROM information_schema.views  WHERE table_schema = 'public';
SELECT proname, prokind        FROM pg_proc  WHERE pronamespace = 'public'::regnamespace ORDER BY 1;
SELECT tgname, tgrelid::regclass AS on_table FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1;
```

`prokind` is `f` for a function and `p` for a procedure.

---

## Views (5)

None of them exposes `payload` or `headers`. A view is the easiest possible way to leak a request
body into a support ticket.

### `v_queue_health` — are we falling behind?

One row. `due_now` is kept apart from `waiting_on_backoff` on purpose: a thousand events sitting out
a retry delay is *not* an incident; a thousand events the workers cannot keep up with is.

```sql
SELECT * FROM v_queue_health;
```

| column | meaning |
|---|---|
| `pending` | not yet processed |
| `due_now` | pending **and** `next_attempt_at <= now()` — the real backlog |
| `waiting_on_backoff` | pending but not due yet — a retry is scheduled |
| `succeeded` / `dead_lettered` | terminal states |
| `oldest_due_age` | how long the oldest due event has been waiting |

### `v_account_reconciliation` — **the important one**

`drift` must be **0 on every row, always**. It is the difference between what the account *says* its
balance is and what the ledger can *prove*. A non-zero drift means an effect was applied twice, or
applied without a ledger row, or a balance was edited by hand.

```sql
SELECT * FROM v_account_reconciliation;

-- The whole correctness claim of this service, as one query. Must return zero rows.
SELECT * FROM v_account_reconciliation WHERE drift <> 0;
```

### `v_dlq_open` — what needs a human

Open dead-letter entries only (`needs_review`, `replaying`), oldest first, with the event context.

```sql
SELECT event_id, event_type, entity_id, attempts_made, status, age, reason FROM v_dlq_open;
```

### `v_event_overview` — one row per event

`has_effect` is the interesting column. An event that is `succeeded` with `has_effect = false` is
**not a bug** — it is a superseded event (FR-10), correctly handled and correctly not applied.

```sql
SELECT id, event_type, status, last_outcome, attempts_recorded, has_effect FROM v_event_overview;

-- Events that were handled but deliberately applied nothing:
SELECT * FROM v_event_overview WHERE status = 'succeeded' AND NOT has_effect;
```

### `v_processing_outcomes` — where the time goes

```sql
SELECT * FROM v_processing_outcomes;
```

The percentile is computed by Postgres over the real distribution, so this is what to check the
Prometheus histogram buckets *against* when they disagree.

---

## Functions (3, plus 4 trigger functions)

### `fn_account_balance(external_ref text) -> bigint`

The balance an account can **prove**, summed from the ledger, ignoring the cached column. Use it to
*check* `account.balance_minor`, never as a substitute — this is O(rows) and the cache is O(1).

```sql
SELECT fn_account_balance('acct_1');
SELECT fn_account_balance('nobody');   -- 0, not an error
```

### `fn_ledger_invariant_ok() -> boolean`

NFR-1 reduced to a single boolean. `false` is an incident.

```sql
SELECT fn_ledger_invariant_ok();
```

### `fn_queue_lag() -> interval`

Age of the oldest event that is due and still waiting. **This**, not the pending count, is the
number to alert on — it is the one a provider would notice.

```sql
SELECT fn_queue_lag();
```

### Trigger functions (not called directly)

`fn_set_updated_at`, `fn_ledger_entry_immutable`, `fn_processing_attempt_immutable`,
`fn_dlq_terminal_is_terminal`. See below.

---

## Procedure (1)

### `sp_purge_history(older_than interval)`

Retention (NFR-12). Returns how much it removed.

```sql
CALL sp_purge_history(interval '90 days');
```

It **never** deletes an event that produced an effect. `ledger_entry.event_id` is `ON DELETE
CASCADE`, so a naive sweep would delete an old event, take its ledger row with it, and leave every
balance wrong. It only removes:

- attempt rows for **succeeded** events older than the cutoff (a dead-lettered event keeps its full
  history — that is the only thing an operator has to go on);
- events that produced **no effect** at all (duplicates, superseded snapshots).

> **Careful when testing this in DataGrip.** `now()` in Postgres is the **transaction's start time**,
> so a row you insert and then try to purge *in the same transaction* is never older than the cutoff
> that transaction computes. Commit first, or age the row explicitly with
> `processed_at = now() - interval '1 hour'`.

---

## Triggers (4)

These enforce invariants the application **cannot**, because the application is not in the room when
someone has a `psql` prompt open at 2am. Every one of them is tested by trying the forbidden thing in
raw SQL.

### `trg_ledger_entry_immutable` — the ledger is append-only

`uq_ledger_entry_event_id` stops an effect being applied *twice*. It does nothing about the row
**afterwards** — and an `UPDATE` or `DELETE` would silently break `balance == SUM(ledger_entry)`,
which is what every correctness claim rests on.

```sql
-- Both of these are REFUSED:
UPDATE ledger_entry SET amount_minor = 999999 WHERE id = 1;
DELETE FROM ledger_entry WHERE id = 1;

-- So is this, which is the case that matters -- the FK is ON DELETE CASCADE, so
-- deleting the event would otherwise take its ledger row with it, silently:
DELETE FROM webhook_event WHERE id = 1;
```

There is **no escape hatch for UPDATE**: a correction is a new, compensating entry. That is what a
ledger is *for*.

`DELETE` has one, because archival is a real requirement and a rule with no legitimate exception just
gets worked around by dropping the trigger. It must be taken deliberately, per transaction:

```sql
BEGIN;
  SET LOCAL app.allow_ledger_delete = 'on';
  DELETE FROM ledger_entry WHERE id = 1;
ROLLBACK;   -- or COMMIT, if you really meant it
```

### `trg_processing_attempt_immutable` — the audit log cannot be rewritten

An audit log that can be edited is a rumour.

```sql
UPDATE processing_attempt SET outcome = 'succeeded' WHERE id = 1;   -- REFUSED
DELETE FROM processing_attempt WHERE id = 1;                        -- allowed
```

`DELETE` **is** allowed here, unlike the ledger, and the asymmetry is deliberate: an attempt row is a
**diagnostic** and has a retention policy; a ledger row is **money** and does not.

### `trg_dlq_terminal_is_terminal` — a settled entry stays settled

`resolved` and `discarded` are terminal. The application already refuses this with a `409`; the
trigger refuses it to everything the application is not — a script, an ad-hoc `UPDATE`, a future
service.

```sql
-- Fine:
UPDATE dead_letter_entry SET status = 'resolved', resolved_at = now() WHERE id = 1;

-- REFUSED -- an entry a human ruled on does not quietly reopen:
UPDATE dead_letter_entry SET status = 'needs_review' WHERE id = 1;

-- Fine: only a STATUS change is guarded. Annotating is ordinary record-keeping.
UPDATE dead_letter_entry SET resolution_note = 'see #4471' WHERE id = 1;
```

### `trg_account_set_updated_at`

Keeps `account.updated_at` honest even when the writer forgets.

---

## A five-minute tour in DataGrip

```sql
-- 1. Everything healthy?
SELECT * FROM v_queue_health;
SELECT fn_ledger_invariant_ok() AS invariant_holds, fn_queue_lag() AS lag;
SELECT * FROM v_account_reconciliation;

-- 2. Break the invariant by hand, and watch it get caught.
UPDATE account SET balance_minor = balance_minor + 1 WHERE external_ref = 'acct_1';
SELECT external_ref, balance_minor, ledger_sum, drift FROM v_account_reconciliation;  -- drift = 1
SELECT fn_ledger_invariant_ok();                                                      -- false
UPDATE account SET balance_minor = balance_minor - 1 WHERE external_ref = 'acct_1';   -- put it back

-- 3. Try to cheat the ledger. Every one of these fails.
UPDATE ledger_entry SET amount_minor = 999999 WHERE id = 1;
DELETE FROM ledger_entry WHERE id = 1;
DELETE FROM webhook_event WHERE id = 1;   -- blocked THROUGH the cascade

-- 4. What is broken, and what did it produce?
SELECT * FROM v_dlq_open;
SELECT id, event_type, status, last_outcome, has_effect FROM v_event_overview;
```

Populate the database first with `make demo` (a duplicate delivery and a reordered one) and
`make send ARGS="--event-type invoice.exploded"` (a poison event, to fill the DLQ).
