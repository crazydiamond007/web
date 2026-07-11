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
# lockfile alone keeps this layer cached across ordinary code edits.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
# `license-files` in pyproject makes this a build input: hatchling refuses to
# build the project without it. Copied here rather than into the layer above, so
# it cannot invalidate the cached dependency install.
COPY LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

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
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "webhook_receiver.api.app:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000"]
