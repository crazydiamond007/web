# Idempotent Webhook Receiver

A service that receives provider webhooks (Stripe-style) and processes each one **exactly once**,
even when the provider delivers the same event several times.

Webhook providers guarantee *at-least-once* delivery. They resend an event if they don't get a `2xx`
back in time — including when we already processed it and only the acknowledgement got lost. This
service accepts those redeliveries safely: it stores the event, returns `200` immediately, and a
background worker applies the business effect exactly once.

The design, requirements, and data model live in [`SPEC.md`](SPEC.md).

## The number

> **20,910 duplicate deliveries under concurrency. 2,466 distinct events. 2,466 ledger entries.
> Zero double-processing. Ingestion p99 of 26 ms at 350 deliveries/s.**

88% of that traffic was a redelivery of something already stored — and a redelivery costs exactly as
much as a first delivery (p99 26 ms vs 27 ms), because deduplication is a single `INSERT ... ON
CONFLICT`, not a lookup followed by a decision. If the duplicate path were the slow one, a provider
retrying *during* an incident would deepen the incident.

Reproduce it with `make load`. Method, the full latency curve, and where the ceiling actually is
(spoiler: the app process, not Postgres) are in [`docs/load-test.md`](docs/load-test.md).

## What it does

| | |
|---|---|
| **Receives** | `POST /v1/webhooks/{source}` — verifies the HMAC-SHA256 signature, stores the event, returns `200`. Ingestion never waits on processing. |
| **Deduplicates** | A redelivered event is recognised by `(source, idempotency_key)` and inserts no new row. |
| **Processes** | A background worker picks up stored events and applies the business effect — in the demo domain, a ledger entry against an account balance. |
| **Applies once** | Each event produces at most one effect, so reprocessing or replaying it changes nothing. |
| **Retries** | Transient failures are retried with exponential backoff and jitter; permanent failures go straight to a dead-letter queue. |
| **Replays** | An authenticated admin endpoint can reprocess events or drain the DLQ. |
| **Reports** | Prometheus metrics at `/metrics`, structured JSON logs, and a queryable event/attempt history. |

## Requirements

| | Version | Needed for |
|---|---|---|
| **Docker** + Compose | any current | Running the stack, and the integration tests |
| **Python** | **3.12** (exactly — pinned in `.python-version`) | Local development |
| **uv** | **0.11+** | Dependency management. `uv.lock` is lockfile revision 3; older uv cannot read it |
| **PostgreSQL** | **16** | Provided by Compose; only needed separately if you run without Docker |

Install [uv](https://docs.astral.sh/uv/): `curl -LsSf https://astral.sh/uv/install.sh | sh`

`uv` will fetch Python 3.12 for you — no system Python needed.

---

## Quick start

```bash
git clone <this-repo> && cd webhook_receiver
cp .env.example .env        # edit it: no secret has a usable default
docker compose up --build
```

That brings up four services in order: `postgres` → `migrate` (applies migrations, then exits) →
`app` and `worker`.

There's a `Makefile` wrapping both ways to run it — `make` on its own lists every target. The
Docker stack is `make up` / `make down`; the local flow is below.

Once it's up, `make demo` sends a signed event twice and then delivers two snapshots out of order,
and `make balance` shows what the worker did with them:

```bash
make demo      # duplicate delivery + reordered delivery, on one account
make balance   # the account, the ledger, and every processing attempt
```

The balance lands on **1000**, the ledger has one row per *applied* event, and the stale snapshot is
recorded as `superseded` rather than applied. To drive it by hand, `make send` takes flags:

```bash
make send ARGS="--count 2"                                       # same event twice -> one row
make send ARGS="--event-type balance.snapshot --balance 900 --sequence 3"
make send ARGS="--skew 400"                                      # stale signature -> 401
```

Check it's up:

```bash
curl localhost:8000/healthz   # {"status":"alive"}
curl localhost:8000/readyz    # {"status":"ready","database":"ok"}
```

API docs are at <http://localhost:8000/docs>.

Tear down, including the database volume:

```bash
docker compose down -v
```

Run more workers — they coordinate through the database, so this is safe:

```bash
docker compose up --scale worker=4
```

---

## Local development

Run the app and worker in your venv, with Postgres in a container (you still need Postgres
somewhere). The `make` targets on the left are exactly the commands on the right:

```bash
make install        # uv sync
make db-up          # docker compose up -d postgres   (or point DATABASE_URL at your own)
make migrate        # uv run alembic upgrade head

make dev            # uv run uvicorn webhook_receiver.api.app:create_app --factory --reload
make worker         # uv run python -m webhook_receiver.worker.main   (a second terminal)
```

Then, from a third terminal, `make send` posts a correctly signed event to the running app.

### Connecting a SQL client (DataGrip, psql, pgAdmin)

Postgres is published on your host while the stack is up, so any client can reach it. There's nothing
extra to create — run **`make db-url`** and it prints exactly what to paste in:

| Field | Value |
|---|---|
| Host | `localhost` |
| Port | whatever `DATABASE_URL` in your `.env` says (`5432` by default) |
| Database | `webhook_receiver` |
| User | `webhook` |
| Password | `webhook` |

Local-dev credentials only — they're set in `docker-compose.yml` and guard nothing. `make psql` opens
a shell on the same database.

**If something already owns port 5432, change the port in `DATABASE_URL` and nothing else.** The
Makefile derives the published port from it, so the container, the local app, and your SQL client
move together.

That clash is worth knowing about, because it does not announce itself. A natively installed Postgres
(common on WSL and Homebrew) keeps 5432, Docker still reports the port as published, your connection
still succeeds — and it lands on *the other server*, which answers:

```
asyncpg.exceptions.InvalidPasswordError: password authentication failed for user "webhook"
```

That is not a credentials bug. It means you are talking to the wrong database. Set the port in
`DATABASE_URL` to `5433`, re-run `make db-up`, and it goes away.

`make up` migrates the database for you. **`make db-up` does not** — run `make migrate` after it, or
your client will connect to a database with no tables.

**Keeping the database up without the app.** `postgres` is `restart: unless-stopped`, so once started
it comes back on its own whenever Docker does — after a reboot, or a Docker Desktop restart. Start it
once with `make db-up` and your SQL client can reach it from then on without the app or worker
running at all. (On Windows this needs *Start Docker Desktop when you log in* enabled in Docker
Desktop's settings; `make down` still stops it deliberately, and it stays stopped.)

### Tests

```bash
uv run pytest tests/unit          # fast, no Docker required
uv run pytest tests/integration   # starts a real Postgres 16 via Testcontainers
uv run pytest                     # everything, with the coverage gate
```

Integration tests need a running Docker daemon and will **fail** without one, rather than skip. Set
`ALLOW_SKIP_INTEGRATION=1` to skip them locally.

### Lint and types

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy                       # runs in --strict mode
```

### Database objects

Migration `0003` adds views, functions, triggers and a retention procedure. `make report` shows the
useful ones:

**Views** — the operator's read model. None of them exposes `payload` or `headers`.

| | |
|---|---|
| `v_queue_health` | Are we falling behind? Keeps `due_now` separate from `waiting_on_backoff` — those are different incidents. |
| `v_account_reconciliation` | `balance_minor` vs `SUM(ledger_entry)`. **`drift` must always be 0.** This is NFR-1 as a query. |
| `v_dlq_open` | Everything still needing a human, oldest first. |
| `v_event_overview` | One row per event with its last outcome and whether it produced an effect. |
| `v_processing_outcomes` | Where the time goes, and what fails. |

**Triggers** — invariants the application cannot defend, because the application isn't in the room
when someone has a `psql` prompt open:

- `ledger_entry` is **append-only**. UPDATE is refused outright (a correction is a compensating
  entry, not a rewrite); DELETE is refused unless you deliberately `SET LOCAL app.allow_ledger_delete
  = 'on'`. It fires *through* the `ON DELETE CASCADE` from `webhook_event`, which is the case that
  would otherwise silently destroy a balance.
- `processing_attempt` cannot be rewritten. An audit log that can be edited is a rumour.
- A `resolved` or `discarded` DLQ entry is **terminal** and cannot reopen.

**Functions**: `fn_account_balance(ref)`, `fn_ledger_invariant_ok()`, `fn_queue_lag()`.

Every object, with copy-pasteable SQL to exercise it, is in
[`docs/database-objects.md`](docs/database-objects.md).

**Procedure**: `CALL sp_purge_history('90 days')` (or `make purge KEEP='30 days'`) — retention. It
never deletes an event that produced an effect, because `ledger_entry.event_id` is `ON DELETE
CASCADE` and a naive sweep would take the ledger with it.

Deliberately **not** here: a trigger maintaining `account.balance` (the effect would then live half
in Python and half in a trigger, and double-apply), and a SQL version of the advisory-lock key (it
would use a different hash from the Python one, take a different lock, and corrupt a balance in
silence — see ADR-0002).

### Migrations

The schema is managed entirely by Alembic. The application never creates tables itself.

```bash
uv run alembic upgrade head                       # apply
uv run alembic downgrade base                     # roll back
uv run alembic revision -m "add thing"            # new migration
uv run alembic upgrade head --sql                 # print SQL without connecting
```

---

## Configuration

All settings come from environment variables. Copy [`.env.example`](.env.example) to `.env` — it
documents every option. There are no hard-coded thresholds in the code.

**Required** (no defaults):

| Variable | Example |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://webhook:webhook@localhost:5432/webhook_receiver` |
| `ADMIN_API_KEY` | any long random string; guards the admin and replay endpoints |
| `WEBHOOK_SECRETS` | `{"stripe":"whsec_..."}` — JSON map of source → HMAC signing key |

**Tunable** (sensible defaults shown):

| Variable | Default | Meaning |
|---|---|---|
| `SIGNATURE_TIMESTAMP_TOLERANCE_SECONDS` | `300` | Reject signatures older/newer than this |
| `MAX_ATTEMPTS` | `5` | Attempts before an event is dead-lettered |
| `BACKOFF_BASE_SECONDS` | `1.0` | Retry delay = `min(cap, base × 2^attempt)`, plus jitter |
| `BACKOFF_CAP_SECONDS` | `300.0` | Maximum retry delay |
| `POLL_BATCH_SIZE` | `100` | Events a worker claims per poll |
| `POLL_INTERVAL_SECONDS` | `1.0` | Worker sleep when there's nothing to do |
| `ADVISORY_LOCK_TIMEOUT_SECONDS` | `5.0` | How long a worker waits for a per-entity lock |
| `ENVIRONMENT` | `local` | `local` gives human-readable logs; anything else gives JSON |
| `LOG_LEVEL` | `INFO` | |
| `JITTER_SEED` | unset | Seeds retry jitter for deterministic tests. Never set in production |

Bad configuration fails at startup rather than at runtime.

---

## Project layout

```
src/webhook_receiver/
  api/         HTTP routes, request schemas, auth, signature verification
  services/    ingest, process, retry, dlq, replay
  domain/      event model, error taxonomy, backoff policy, handlers
  adapters/    SQLAlchemy models, repositories, advisory lock, clock, rng
  worker/      poll loop and dispatch
  obs/         logging, correlation id, metrics
  config.py    all settings
migrations/    alembic
tests/
  unit/        no database
  integration/ real Postgres via Testcontainers
  load/        locust
docs/adr/      architecture decision records
```


## API

Ingestion authenticates by **HMAC signature**; the admin routes authenticate by **API key**
(`X-Admin-Key`). They are deliberately different: a provider has no account here, and an operator has
no signing secret.

| | |
|---|---|
| `POST /v1/webhooks/{source}` | Ingest a delivery. Signature-authenticated. |
| `GET /v1/admin/events` | Filter events by status, source, type, entity, time. |
| `GET /v1/admin/events/{id}` | One event with its full attempt history. |
| `GET /v1/admin/dlq` | The dead-letter queue. |
| `POST /v1/admin/dlq/{id}/resolve` \| `/discard` | Triage an entry. Both are terminal. |
| `POST /v1/admin/replay` | Re-process events, the DLQ, or a time range. |
| `GET /metrics` | Prometheus. The **worker** serves its own on `:9100` — see below. |
| `GET /healthz` \| `/readyz` | Liveness and readiness. |

The admin routes never return `payload` or `headers`. It's a support tool, and a support tool that
prints the raw body turns every screenshot pasted into a ticket into a leak.

```bash
curl -H "X-Admin-Key: $ADMIN_API_KEY" localhost:8000/v1/admin/dlq
curl -H "X-Admin-Key: $ADMIN_API_KEY" -X POST localhost:8000/v1/admin/replay \
     -H 'content-type: application/json' -d '{"dead_lettered": true, "reason": "handler fixed"}'
```

### Metrics live in two processes

`make metrics` scrapes the app (ingested, rejected, ingest latency). `make worker-metrics` scrapes
the worker (processed, retried, dead-lettered, processing latency).

They are separate because Prometheus scrapes a *process*, not an application, and the counters that
matter most are incremented in the worker. In a real deployment both are scraped on the container
network; locally the worker's port is ephemeral so that `make up-scale` (four workers) still works.

## What works today

| | |
|---|---|
| **Event types** | `balance.credited`, `balance.debited`, `balance.snapshot` |
| **Guarantees** | Signature + timestamp verification, deduplicated ingestion, exactly-once effects, per-entity serialisation, out-of-order handling, bounded retries with jittered backoff, dead-lettering, idempotent replay |

**Deployment:** [`infra/`](infra/) holds Terraform for Fargate + RDS. It is **validated but never
applied** — there's no AWS account behind it, and `terraform plan` has never run. It's there because
"how would you deploy this?" deserves an answer you can review line by line, and a README claiming a
deployment that never happened would be the one untrue thing in this repo.

## Further reading

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the design, and the five constraints that do the work
- [`docs/load-test.md`](docs/load-test.md) — the number, the method, and where the ceiling actually is
- [`docs/runbook.md`](docs/runbook.md) — what to do when it's 3am and you've been paged
- [`docs/database-objects.md`](docs/database-objects.md) — the views, triggers, and guards, with SQL to try them
- [`docs/adr/`](docs/adr/) — eight architecture decision records, including the things I *didn't* build
- [`infra/README.md`](infra/README.md) — the deployment, what it costs, and what's missing
- [`SPEC.md`](SPEC.md) — the original requirements
