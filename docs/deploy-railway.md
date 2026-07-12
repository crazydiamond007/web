# Deploying to Railway

Three services, one repository, one Postgres:

| Service    | What it is                    | Config file            | Public? |
| ---------- | ----------------------------- | ---------------------- | ------- |
| `Postgres` | Railway's managed Postgres    | —                      | no      |
| `app`      | the ingestion API + admin API | `railway.toml`         | yes     |
| `worker`   | the queue processor           | `railway.worker.toml`  | no      |

`app` and `worker` build the **same image** from the **same Dockerfile** and differ
only in their start command. That is not a shortcut — the worker imports the same
handlers and the same `process_event` the replay endpoint calls, and it matters
that they cannot drift apart.

## Before you start

- A Railway account on the Hobby plan.
- This repository pushed to GitHub.
- Two secrets generated locally. Keep them; you will paste them into Railway and
  they must match the ones in your `.env` if you want to send test deliveries
  from your laptop.

```bash
openssl rand -hex 32   # -> ADMIN_API_KEY
openssl rand -hex 32   # -> the signing secret for the "stripe" source
```

## Step 1 — Project and database

1. **New Project → Deploy PostgreSQL.** Let it finish provisioning.
2. Leave the service named **`Postgres`**. The variable references below use that
   name literally; rename it and you must rename them too.

Nothing else to do here. The schema is not created by hand — `alembic upgrade head`
runs as the app's pre-deploy command (step 3), and it is the only thing that ever
writes DDL.

## Step 2 — The variables

Set these on **both** `app` and `worker`. The Railway variable pane has a raw
editor; paste the block, then substitute your two secrets.

```env
DATABASE_URL=${{Postgres.DATABASE_URL}}
ADMIN_API_KEY=<the first openssl value>
WEBHOOK_SECRETS={"stripe":"<the second openssl value>"}
ENVIRONMENT=production
LOG_LEVEL=INFO
```

Both services genuinely need all of it. The worker never verifies a signature, but
`Settings` validates every field at construction, and `admin_api_key` has no
default — a worker without it does not start.

Notes on the two that bite:

- **`DATABASE_URL`** is a cross-service reference, not a literal. Railway resolves
  `${{Postgres.DATABASE_URL}}` to the **private** DSN
  (`postgres.railway.internal`), so the traffic never leaves the project and you
  are not billed egress for it. Railway publishes it as `postgresql://…`, with no
  driver — SQLAlchemy would resolve the dialect's *default* driver, psycopg2,
  which is synchronous and not installed. `Settings._normalise_driver` rewrites the
  scheme to `postgresql+asyncpg://`, which is why you can paste the reference
  verbatim instead of hand-assembling the URL out of `PGUSER`/`PGPASSWORD`/… and
  hoping the generated password contains nothing that needs percent-encoding.
- **`WEBHOOK_SECRETS`** is a JSON object, one entry per source. The key is the
  `{source}` in `POST /v1/webhooks/{source}`.

If you would rather not maintain the same block twice, define them once under
**Project Settings → Shared Variables** and reference them as `${{shared.NAME}}`
from each service. The failure this avoids is real: an `app` and a `worker`
pointed at two different databases will both look completely healthy.

## Step 3 — The `app` service

1. **New → GitHub Repo →** this repository.
2. Name it **`app`**.
3. **Settings → Config-as-code:** leave it as `railway.toml` (the default).
4. Add the variables from step 2.
5. Deploy.

`railway.toml` already sets everything that matters:

- `preDeployCommand = ["alembic upgrade head"]` — runs in its own container, on the
  private network, with the service's variables, **before** any new container takes
  traffic. A non-zero exit aborts the release, so a bad migration takes the deploy
  down with it rather than promoting an app onto a schema it cannot use.
- `healthcheckPath = "/healthz"` — liveness, never `/readyz`. `/readyz` reports on
  Postgres, so wiring it to the health check would let a database blip get a
  perfectly healthy container killed and rescheduled.
- `sleepApplication = false`.

Migrations run **here and nowhere else**. The worker has no pre-deploy command on
purpose; two services racing `alembic upgrade head` against one database is a real
way to corrupt a schema.

Which means the worker has no ordering guarantee against the migration — the two
services deploy at once, and on a cold start the worker gets there first and finds
an empty database. It handles this itself: it logs `worker.awaiting_schema` and
polls until the tables appear, then `worker.schema_ready`. Nothing for you to
sequence. If the schema never arrives (a typo'd `DATABASE_URL`, a pre-deploy that
was never wired up) it exits loudly after `schema_wait_timeout_seconds`, rather
than sitting there looking healthy.

## Step 4 — The `worker` service

1. **New → GitHub Repo →** the same repository again.
2. Name it **`worker`**.
3. **Settings → Config-as-code → `railway.worker.toml`.** ← Do not skip this.
4. Add the same variables from step 2.
5. Confirm **no public domain** is generated for it.
6. Deploy.

If you forget step 3, the worker silently inherits `railway.toml` and boots as a
second copy of the API. Nothing then drains the queue, and nothing tells you so:
the API keeps returning 202, every delivery is durably stored, and not one event
is ever processed.

The two settings in that file that are load-bearing:

- **`sleepApplication = false`.** A sleeping Railway service is woken by inbound
  HTTP. The worker receives no inbound HTTP — it polls Postgres. Enable sleep and
  it goes to sleep and *nothing can ever wake it*, because the requests all go to
  `app`. Green health checks, 202s all the way, zero events processed.
- **No `healthcheckPath`.** The worker serves HTTP only on the metrics port (9100),
  never on the port Railway probes. Give it a health check and it fails every
  probe, gets killed, restarts, and fails again — forever.

## Step 5 — The public domain

On `app` only: **Settings → Networking → Generate Domain.** Railway assigns the
port and injects `$PORT`; the Dockerfile's `CMD` binds `${PORT:-8000}`, so this
needs no configuration.

## Step 6 — Prove it works

```bash
BASE=https://<your-app>.up.railway.app
KEY=<ADMIN_API_KEY>

curl -fsS $BASE/healthz     # {"status":"alive"}
curl -fsS $BASE/readyz      # {"status":"ready","database":"ok"}  <- proves the private DSN resolved
```

`/readyz` is the one that matters here. It opens a real connection, so a 200 means
the driver normalisation worked, the private network resolved, and the migration
ran.

Now the actual claim of the service. `send_webhook.py` signs with the secret from
your **local `.env`**, so set `WEBHOOK_SECRETS` there to the same value you put in
Railway, then send one event **twice**:

```bash
uv run python scripts/send_webhook.py --url $BASE --count 2
```

Expect `202 accepted` then `200 duplicate` — the same event id, the second
delivery deduplicated by the unique constraint rather than by a lookup.

Then confirm the worker actually consumed it, and that the duplicate produced no
second effect:

```bash
curl -fsS -H "X-Admin-Key: $KEY" "$BASE/v1/admin/events?status=succeeded" | jq
curl -fsS -H "X-Admin-Key: $KEY" "$BASE/v1/admin/dlq" | jq        # expect []
```

The interactive API docs are at `$BASE/docs`.

## Operating it

- **The queue is the health signal**, not CPU. Connect DataGrip or `psql` to the
  Postgres service's `DATABASE_PUBLIC_URL` (Railway's TCP proxy) and read
  `v_queue_health`; `waiting_on_backoff` is normal, a growing `due_now` is not.
  `v_account_reconciliation.drift` must always be `0`. See `docs/database-objects.md`.
- **The worker rides out a database outage; it does not exit.** A failover, a
  Postgres restart, or a private-network blip makes it log `worker.poll_failed` and
  back off with full jitter until the database answers again, then `worker.recovered`.
  What still kills it is an *unclassified* exception — a bug in our own code —
  because looping on a bug forever would bury the stack trace instead of surfacing
  it. So a worker that has genuinely died has died of something worth reading.
- **A dead worker looks exactly like an idle one from the outside.** If it does
  exhaust `restartPolicyMaxRetries`, nothing turns red — the API keeps returning
  202. A climbing `due_now` in `v_queue_health` is how you find out.
- **Logs are structured JSON** and carry a correlation id. Payloads and secrets are
  never in them, by design (NFR-6) — so a log line will tell you *which* event
  failed and not what was in it. `GET /v1/admin/events/{id}` is where you look next.
- **`/metrics` on `app`** is Prometheus-formatted and public. The worker's counters
  (`processed`, `retried`, `dead_lettered`) live in the *worker's* process on port
  9100, which nothing on Railway scrapes. If you want them, that is what a Grafana
  Cloud agent or a Railway Prometheus service would be for.

## What it costs

Three always-on services plus a volume. Idle, the two Python processes sit at
roughly 150 MB and 120 MB, and Postgres at around 250 MB — call it half a gigabyte
of billed memory with near-zero CPU. That lands **a little past** the $5 of usage
the Hobby plan includes; budget a few dollars a month rather than expecting the
credit to absorb it.

The obvious lever is `numReplicas` on the worker — but leave it at 1. One worker
drains this queue comfortably, and the scaling story (`SKIP LOCKED` + advisory
locks, no leader, no partition assignment) is already proven by the load test in
`docs/load-test.md`. Paying Railway to re-prove it is not a good use of $5.

## The failures worth knowing in advance

| Symptom | Cause |
| --- | --- |
| Events pile up in `pending`, everything looks healthy | `sleepApplication` left on for the worker, or the worker booted with `railway.toml` and is a second API |
| Worker restart-loops forever | a `healthcheckPath` was set on it |
| Deploy is green, first webhook 500s | `DATABASE_URL` set to a literal rather than `${{Postgres.DATABASE_URL}}`, pointing at nothing |
| Every delivery returns 401 | `WEBHOOK_SECRETS` in Railway does not match the `.env` you are signing with. The 401 is deliberately identical for an unknown source, a bad signature, and a stale timestamp — it tells you as little as it tells an attacker |
| Deploy fails at pre-deploy | the migration failed. That is the system working: read the pre-deploy logs, and note that no app container ever took traffic against the broken schema |
