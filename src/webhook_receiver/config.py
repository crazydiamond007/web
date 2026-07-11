"""Application configuration.

SPEC §NFR-11: every threshold comes from config. No magic numbers in the code.
SPEC §NFR-6: secrets come from the environment only, and never reach a log line.

Secrets are typed ``SecretStr`` so that an accidental ``repr`` of ``Settings``
in a log or traceback prints ``**********`` instead of the signing key.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Final

from pydantic import Field, PostgresDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The provider signs `timestamp.payload`; ±5 minutes is the window Stripe uses
# and is the smallest value that tolerates realistic clock skew between hosts.
DEFAULT_TIMESTAMP_TOLERANCE_SECONDS: Final[int] = 300


class Environment(StrEnum):
    """Deployment environment. Gates behaviour that must never run in prod."""

    LOCAL = "local"
    CI = "ci"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class Settings(BaseSettings):
    """Typed, validated application configuration.

    Bounds are enforced here rather than at the call site: a ``max_attempts`` of
    0 or a ``backoff_cap`` below ``backoff_base`` are configuration bugs, and
    the process should refuse to start rather than dead-letter every event or
    retry forever.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    environment: Environment = Environment.LOCAL
    log_level: LogLevel = LogLevel.INFO

    # --- Database ------------------------------------------------------------

    database_url: PostgresDsn = Field(
        description="Async SQLAlchemy DSN, e.g. postgresql+asyncpg://user:pw@host:5432/db",
    )
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)

    # --- Ingestion / signature verification (FR-3, FR-4) ---------------------

    webhook_secrets: dict[str, SecretStr] = Field(
        default_factory=dict,
        description='Per-source HMAC signing keys, as JSON: {"stripe": "whsec_..."}',
    )
    signature_timestamp_tolerance_seconds: int = Field(
        default=DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
        gt=0,
        description="FR-4: reject signed timestamps outside ±this window.",
    )
    max_payload_bytes: int = Field(
        default=1_048_576,
        gt=0,
        description="Reject bodies larger than this before hashing them.",
    )

    # --- Admin API (FR-20) ---------------------------------------------------

    admin_api_key: SecretStr = Field(
        description="FR-20: required by admin, replay, and DLQ endpoints.",
    )
    admin_page_size: int = Field(
        default=50,
        gt=0,
        le=500,
        description="FR-18: default rows per admin listing.",
    )
    replay_max_batch: int = Field(
        default=100,
        gt=0,
        description=(
            "FR-16: most events one replay request may touch. A bound, not a "
            "preference: replay is synchronous and takes an advisory lock per "
            "event, so an unbounded 'replay everything' is a self-inflicted outage."
        ),
    )

    # --- Worker poll (FR-7) --------------------------------------------------

    worker_metrics_port: int = Field(
        default=9100,
        gt=0,
        le=65535,
        description=(
            "FR-19: the worker serves its own /metrics here. The processed, "
            "retried and dead-lettered counters live in the worker process, and a "
            "counter nothing can scrape is not a metric."
        ),
    )

    poll_batch_size: int = Field(
        default=100,
        gt=0,
        description="Rows claimed per FOR UPDATE SKIP LOCKED batch.",
    )
    poll_interval_seconds: float = Field(
        default=1.0,
        gt=0,
        description="Sleep between polls when the last batch came back empty.",
    )
    advisory_lock_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        description="FR-9: give up waiting on pg_advisory_xact_lock after this long.",
    )

    # --- Retry and backoff (FR-12, FR-13) ------------------------------------

    max_attempts: int = Field(
        default=5,
        ge=1,
        description="FR-13: dead-letter the event once attempts reach this bound.",
    )
    backoff_base_seconds: float = Field(
        default=1.0,
        gt=0,
        description="FR-12: delay = min(cap, base * 2**attempt), then full jitter.",
    )
    backoff_cap_seconds: float = Field(
        default=300.0,
        gt=0,
        description="FR-12: ceiling on the pre-jitter delay.",
    )
    jitter_seed: int | None = Field(
        default=None,
        description="SPEC §6.4: seed the jitter RNG for deterministic tests. None = entropy.",
    )

    @model_validator(mode="after")
    def _cap_bounds_base(self) -> Settings:
        if self.backoff_cap_seconds < self.backoff_base_seconds:
            msg = (
                f"backoff_cap_seconds ({self.backoff_cap_seconds}) must be >= "
                f"backoff_base_seconds ({self.backoff_base_seconds}); otherwise the "
                f"cap would shorten the first retry rather than bound the last."
            )
            raise ValueError(msg)
        return self

    def secret_for_source(self, source: str) -> SecretStr | None:
        """Return the signing key for ``source``, or ``None`` if unconfigured.

        Returning ``None`` rather than raising lets the caller answer an unknown
        source with the same 401 as a bad signature, so probing for configured
        sources tells an attacker nothing.
        """
        return self.webhook_secrets.get(source)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached because ``Settings`` reads the environment and the filesystem. Tests
    override it through FastAPI's dependency system, or by calling
    ``get_settings.cache_clear()``.
    """
    return Settings()
