# Idempotent Webhook Receiver

A service that receives provider webhooks (Stripe-style) and processes each one **exactly once**,
even when the provider delivers the same event several times.

Webhook providers guarantee *at-least-once* delivery. They resend an event if they don't get a `2xx`
back in time — including when we already processed it and only the acknowledgement got lost. This
service accepts those redeliveries safely: it stores the event, returns `200` immediately, and a
background worker applies the business effect exactly once.

The design, requirements, and data model live in [`SPEC.md`](SPEC.md).

> **Status: Day 0 of 4.** Foundation only. `GET /healthz` and `GET /readyz` are live; the full
> database schema, config, container, and CI are in place. Ingestion (`POST /v1/webhooks/{source}`),
> the worker, retries, and the DLQ arrive in Days 1–3. See [Status](#status).

---

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

Without Docker for the app itself (you still need Postgres somewhere):

```bash
uv sync --extra dev                 # creates .venv, installs everything
docker compose up -d postgres       # or point DATABASE_URL at your own

uv run alembic upgrade head         # apply migrations

uv run uvicorn webhook_receiver.api.app:create_app --factory --reload
uv run python -m webhook_receiver.worker.main    # in a second terminal
```

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

## Status

| Slice | Delivers | State |
|---|---|---|
| Day 0 | Scaffold, schema, config, `/healthz`, `/readyz`, Docker, CI | ✅ Done |
| Day 1 | `POST /v1/webhooks/{source}`, signature verification, dedup | Not started |
| Day 2 | Worker, per-entity locking, effect ledger, ordering | Not started |
| Day 3 | Retries, DLQ, replay, admin API, `/metrics` | Not started |
| Day 4 | Load test, AWS deploy, `ARCHITECTURE.md`, runbook | Not started |

Endpoints live today: `GET /healthz`, `GET /readyz`.

The database schema is already complete — all six tables and four enum types, with every constraint
the design depends on.

## Further reading

- [`SPEC.md`](SPEC.md) — requirements, data model, architecture
- [`docs/adr/`](docs/adr/) — architecture decision records
  - [0001](docs/adr/0001-postgres-as-queue.md) — why Postgres is the queue, not a broker
  - [0006](docs/adr/0006-superseded-attempt-outcome.md) — adding `superseded` to `attempt_outcome`
- `ARCHITECTURE.md` — how the pieces fit and why (Day 4)
- `docs/runbook.md` — operational procedures (Day 4)
