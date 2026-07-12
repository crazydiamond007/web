# syntax=docker/dockerfile:1.7
#
# Multi-stage, non-root (SPEC §5, §NFR-6, §NFR-10).
#
# The build stage owns uv and the compilers; the runtime stage receives only the
# resolved virtualenv and the source. Nothing that can build a wheel survives
# into the image that faces the internet.

ARG PYTHON_VERSION=3.12

# --- Stage 1: resolve dependencies -------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# Must be able to read `uv.lock`, which is lockfile revision 3. uv 0.5 cannot;
# it fails `uv sync --frozen`. Keep this major.minor in step with the uv that
# writes the lockfile.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies change far less often than source. Installing them from the
# lockfile alone keeps this layer cached across ordinary code edits, and that
# layer cache -- not a mount -- is what makes an ordinary rebuild fast.
#
# No `--mount=type=cache` here, deliberately, and it took two attempts to accept
# that. BuildKit is happy with a bare cache mount. Railway's parser demands an
# `id`, and then demands that the id carry an `s/<service id>-` prefix, and then
# forbids variables in it. `app` and `worker` are two services built from this one
# Dockerfile, so there is no literal id that is correct for both: the constraint
# is unsatisfiable without forking the Dockerfile per service.
#
# Which is a bad trade for what the mount actually bought. It only ever helped on
# a *local* rebuild after `uv.lock` moved -- CI runners are ephemeral, so the mount
# never survived a job there either. That is worth a few seconds of re-downloaded
# wheels, and not worth a second Dockerfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
# `license-files` in pyproject makes this a build input: hatchling refuses to
# build the project without it. Copied here rather than into the layer above, so
# it cannot invalidate the cached dependency install.
COPY LICENSE ./
RUN uv sync --frozen --no-dev

# --- Stage 2: runtime ---------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# A fixed uid/gid so bind-mounted volumes have predictable ownership.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/migrations /app/migrations
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini

USER app

EXPOSE 8000

# Liveness only. Readiness is the orchestrator's business, and a container that
# marks itself unhealthy on a database blip would be restarted rather than
# drained -- exactly the failure /healthz vs /readyz exists to prevent.
#
# The probe reads $PORT for the same reason the CMD does. Pinned to 8000, it would
# fail forever anywhere the platform assigns the port -- and a liveness probe that
# cannot pass is not a warning, it is a restart loop.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; p=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=2).status == 200 else 1)"]

# `sh -c`, because the port must be expanded at *run* time. A PaaS (Railway,
# Heroku, Cloud Run) picks the port and injects $PORT; Compose and a bare
# `docker run` leave it unset and want 8000. Hardcoding 8000 binds a port the
# platform's proxy is not routing to, and the deploy then fails its health check
# while the application behind it runs perfectly well -- one of the more
# infuriating ways to lose an afternoon.
#
# `exec` is not decoration. Without it, sh remains PID 1 and does not forward
# SIGTERM, so a rolling deploy would leave uvicorn to be SIGKILLed once the grace
# period expired, dropping in-flight requests. For an ingestion endpoint that
# means a delivery the provider was told we accepted (202) and which never
# reached the queue -- precisely the event-loss this service exists to prevent.
# With `exec`, uvicorn *is* PID 1 and shuts down gracefully.
CMD ["sh", "-c", \
     "exec uvicorn webhook_receiver.api.app:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
