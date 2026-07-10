"""Integration fixtures: a real Postgres 16, per SPEC §6.5.

Idempotency, advisory locks, and SKIP LOCKED are database behaviours. Mocking
them would test the mock. Every test in this package talks to a real server.

The container is module-scoped: starting Postgres costs a couple of seconds and
the schema is rebuilt per-test where isolation demands it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from testcontainers.postgres import PostgresContainer

POSTGRES_IMAGE = "postgres:16-alpine"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _require_docker() -> None:
    """Fail loudly rather than silently skipping the tests that prove correctness.

    An integration suite that quietly skips itself when Docker is absent is worse
    than no suite: CI stays green while the guarantees go unverified. Set
    ALLOW_SKIP_INTEGRATION=1 to opt out locally.
    """
    if os.environ.get("ALLOW_SKIP_INTEGRATION") == "1":
        pytest.skip("ALLOW_SKIP_INTEGRATION=1: skipping Docker-backed tests by request")


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    _require_docker()
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    """Async DSN for the running container."""
    return str(postgres_container.get_connection_url())


@pytest.fixture
def alembic_config(database_url: str) -> AlembicConfig:
    config = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    # -x url=... is how env.py learns the DSN without a committed password.
    config.cmd_opts = None
    config.attributes["url"] = database_url
    return config
