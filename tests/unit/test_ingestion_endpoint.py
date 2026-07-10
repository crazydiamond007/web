"""Endpoint paths that reject *before* the database is touched.

401 (auth), 413 (too large), and 400 (unroutable) all return before
`ingest_event` opens a session, so they need no Postgres and can be verified
here without Docker. The paths that assert row counts live in the integration
suite, where a real dedup constraint exists to assert against.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from webhook_receiver.api.app import create_app
from webhook_receiver.api.signature import SIGNATURE_HEADER, expected_signature
from webhook_receiver.config import Settings

# No database is reached on any path under test, so an unreachable DSN is fine
# and keeps these tests Docker-free.
UNREACHABLE_DSN = "postgresql+asyncpg://user:pw@127.0.0.1:1/nodb"
SOURCE = "stripe"
SECRET = "whsec_unit"
MAX_BYTES = 1024

BODY = (
    b'{"id":"evt_1","type":"balance.credited","occurred_at":"2026-07-10T12:00:00Z",'
    b'"entity":{"type":"account","id":"acct_1"},"data":{"amount":500}}'
)


def _sig(body: bytes, *, at: int | None = None, secret: str = SECRET) -> str:
    timestamp = at if at is not None else int(time.time())
    return f"t={timestamp},v1={expected_signature(secret, timestamp, body)}"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(
        Settings(
            database_url=UNREACHABLE_DSN,
            admin_api_key="k",
            webhook_secrets={SOURCE: SECRET},
            max_payload_bytes=MAX_BYTES,
            _env_file=None,
        )
    )
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
        app.router.lifespan_context(app),
    ):
        yield http


async def test_a_missing_signature_is_401(client: AsyncClient) -> None:
    response = await client.post(f"/v1/webhooks/{SOURCE}", content=BODY)
    assert response.status_code == 401


async def test_a_bad_signature_is_401(client: AsyncClient) -> None:
    header = {SIGNATURE_HEADER: _sig(BODY, secret="whsec_wrong")}
    response = await client.post(f"/v1/webhooks/{SOURCE}", content=BODY, headers=header)
    assert response.status_code == 401


async def test_an_unknown_source_is_401_with_the_same_body(client: AsyncClient) -> None:
    # NFR-6: identical to a bad-signature rejection, so probing tells nothing.
    unknown = await client.post(
        "/v1/webhooks/paypal", content=BODY, headers={SIGNATURE_HEADER: _sig(BODY)}
    )
    bad_sig = await client.post(
        f"/v1/webhooks/{SOURCE}",
        content=BODY,
        headers={SIGNATURE_HEADER: _sig(BODY, secret="whsec_wrong")},
    )

    assert unknown.status_code == bad_sig.status_code == 401
    assert unknown.json() == bad_sig.json()


async def test_an_oversized_body_is_413_before_any_hashing(client: AsyncClient) -> None:
    oversized = b'{"pad":"' + b"x" * (MAX_BYTES + 1) + b'"}'
    # No signature header at all: the size check must fire first, proving we do
    # not hash an attacker-sized body before bounding it.
    response = await client.post(f"/v1/webhooks/{SOURCE}", content=oversized)
    assert response.status_code == 413


async def test_a_signed_but_unroutable_body_is_400(client: AsyncClient) -> None:
    unroutable = b'{"id":"evt_1","type":"x"}'  # missing entity and occurred_at
    header = {SIGNATURE_HEADER: _sig(unroutable)}
    response = await client.post(f"/v1/webhooks/{SOURCE}", content=unroutable, headers=header)

    assert response.status_code == 400
    # The 400 body names fields, never the payload values (NFR-6).
    assert "evt_1" not in response.text
