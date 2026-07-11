"""The event as the domain sees it, once it has been authenticated and parsed.

Pure Python: no FastAPI, no pydantic, no SQLAlchemy (SPEC §4). The API layer
validates the wire format and hands one of these inward; the persistence adapter
takes one and writes a row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

type JsonObject = dict[str, object]


@dataclass(frozen=True, slots=True)
class IncomingEvent:
    """An authenticated delivery, ready to be persisted.

    Frozen: between verification and the insert, nothing may alter what we
    verified. `signature_verified` is carried explicitly rather than assumed,
    because a replayed event (FR-16) re-enters this path without a signature and
    the row must say so honestly.
    """

    source: str
    external_id: str
    idempotency_key: str
    event_type: str
    entity_type: str
    entity_id: str
    payload: JsonObject
    headers: JsonObject
    signature_verified: bool
    occurred_at: datetime
    provider_sequence: int | None


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """A persisted event, as a handler sees it (FR-7, FR-8).

    The worker reads a row and hands one of these to a handler. It is a strict
    subset of the row: no `status`, no `next_attempt_at`, no `last_error`. A
    handler decides *what this event means*, and queue bookkeeping is none of its
    business -- a handler that could see the retry state would eventually branch
    on it.

    `provider_sequence` is the ordering key where the provider gives us one
    (FR-10). It is `None` for providers that don't, which is why the guard lives
    with the state-setting effect rather than being applied blindly to every
    event.
    """

    id: int
    source: str
    event_type: str
    entity_type: str
    entity_id: str
    payload: JsonObject
    occurred_at: datetime
    provider_sequence: int | None
    attempt_count: int


class MalformedPayloadError(Exception):
    """The body verified, but is not an event we can route.

    Distinct from a signature failure: this request genuinely came from the
    provider, so it earns a `400` rather than a `401`. It cannot be persisted --
    without an `external_id` there is no idempotency key, and therefore no way to
    deduplicate a redelivery of it.
    """
