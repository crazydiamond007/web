"""The ingestion endpoint, end to end against a real Postgres (FR-1..FR-5).

The two assertions SPEC §7 makes the Day 1 gate are here and deliberately
literal:

* a **duplicate delivery** inserts one row and answers `200` both times;
* a **tampered body** is answered `401` and leaves zero rows.

Everything is real: real HMAC, real dedup constraint, real transaction. The
count queries hit the same database the endpoint wrote to, so a dedup that only
worked in the ORM's imagination would fail here.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from webhook_receiver.api.app import create_app
from webhook_receiver.api.schemas import IDEMPOTENCY_KEY_HEADER
from webhook_receiver.api.signature import SIGNATURE_HEADER, expected_signature
from webhook_receiver.config import Settings

pytestmark = pytest.mark.integration

SOURCE = "stripe"
SECRET = "whsec_integration"

BODY = (
    b'{"id":"evt_1","type":"balance.credited","occurred_at":"2026-07-10T12:00:00Z",'
    b'"entity":{"type":"account","id":"acct_1"},"data":{"amount":500}}'
)


def _signature_header(body: bytes, *, at: int | None = None) -> str:
    timestamp = at if at is not None else int(time.time())
    return f"t={timestamp},v1={expected_signature(SECRET, timestamp, body)}"


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        admin_api_key="test-admin-key",
        webhook_secrets={SOURCE: SECRET},
        _env_file=None,
    )


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """A second engine, for asserting row counts independently of the endpoint."""
    eng = create_async_engine(str(settings.database_url))
    yield eng
    await eng.dispose()


@pytest.fixture
async def client(
    settings: Settings,
    migrated_schema: None,  # ordering dependency: schema exists before any request
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
        app.router.lifespan_context(app),
    ):
        yield http


async def _event_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(sa.text("SELECT count(*) FROM webhook_event"))
        return int(result.scalar_one())


async def test_a_valid_delivery_is_accepted_and_persisted(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    response = await client.post(
        f"/v1/webhooks/{SOURCE}",
        content=BODY,
        headers={SIGNATURE_HEADER: _signature_header(BODY), "content-type": "application/json"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["duplicate"] is False
    assert await _event_count(engine) == 1


async def test_a_duplicate_delivery_inserts_one_row_and_returns_two_200s(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """The Day 1 headline (FR-5). The provider redelivers; we stay idempotent."""
    header = {SIGNATURE_HEADER: _signature_header(BODY), "content-type": "application/json"}

    first = await client.post(f"/v1/webhooks/{SOURCE}", content=BODY, headers=header)
    second = await client.post(f"/v1/webhooks/{SOURCE}", content=BODY, headers=header)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    # Same row both times: the response id is stable across the redelivery.
    assert first.json()["event_id"] == second.json()["event_id"]
    assert await _event_count(engine) == 1


async def test_a_tampered_body_is_rejected_and_persists_nothing(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """The other Day 1 headline (FR-3). A signature over a different body."""
    header = {SIGNATURE_HEADER: _signature_header(BODY), "content-type": "application/json"}
    tampered = BODY.replace(b'"amount":500', b'"amount":999999')

    response = await client.post(f"/v1/webhooks/{SOURCE}", content=tampered, headers=header)

    assert response.status_code == 401
    assert await _event_count(engine) == 0


async def test_a_stale_timestamp_is_rejected(client: AsyncClient, engine: AsyncEngine) -> None:
    """FR-4: a correctly signed but old delivery is still refused, and stored 0."""
    stale = int(time.time()) - 3600
    header = {
        SIGNATURE_HEADER: _signature_header(BODY, at=stale),
        "content-type": "application/json",
    }

    response = await client.post(f"/v1/webhooks/{SOURCE}", content=BODY, headers=header)

    assert response.status_code == 401
    assert await _event_count(engine) == 0


async def test_an_unknown_source_is_rejected_indistinguishably(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """FR-3 / NFR-6: an unconfigured source answers the same 401 as a bad MAC."""
    response = await client.post(
        "/v1/webhooks/unconfigured",
        content=BODY,
        headers={SIGNATURE_HEADER: _signature_header(BODY), "content-type": "application/json"},
    )

    assert response.status_code == 401
    assert await _event_count(engine) == 0


async def test_a_signed_but_unroutable_body_is_a_400_not_a_401(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """The 400/401 split: authentication passed, routing did not."""
    unroutable = b'{"id":"evt_1","type":"x"}'  # no entity, no occurred_at
    header = {
        SIGNATURE_HEADER: _signature_header(unroutable),
        "content-type": "application/json",
    }

    response = await client.post(f"/v1/webhooks/{SOURCE}", content=unroutable, headers=header)

    assert response.status_code == 400
    assert await _event_count(engine) == 0


async def test_an_explicit_idempotency_key_deduplicates_across_distinct_events(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """FR-5: the key, not the payload, decides identity.

    Two different event ids sharing one `Idempotency-Key` collapse to one row --
    the mechanism a client uses to retry a request it never saw answered.
    """
    first_body = BODY
    second_body = BODY.replace(b'"id":"evt_1"', b'"id":"evt_2"')

    def _headers(body: bytes) -> dict[str, str]:
        return {
            SIGNATURE_HEADER: _signature_header(body),
            IDEMPOTENCY_KEY_HEADER: "shared-key",
            "content-type": "application/json",
        }

    first = await client.post(
        f"/v1/webhooks/{SOURCE}", content=first_body, headers=_headers(first_body)
    )
    second = await client.post(
        f"/v1/webhooks/{SOURCE}", content=second_body, headers=_headers(second_body)
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert await _event_count(engine) == 1
