"""Correlation-id middleware (NFR-5), verified on `/healthz` so no DB is needed.

The middleware's contract is small and testable without a server dependency:
every response carries an `X-Request-Id`, an inbound one is honoured, and a
hostile one is not copied through verbatim.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from webhook_receiver.api.app import create_app
from webhook_receiver.api.middleware import CORRELATION_ID_HEADER
from webhook_receiver.config import Settings

UNREACHABLE_DSN = "postgresql+asyncpg://user:pw@127.0.0.1:1/nodb"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(database_url=UNREACHABLE_DSN, admin_api_key="k", _env_file=None))
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
        app.router.lifespan_context(app),
    ):
        yield http


async def test_every_response_carries_a_correlation_id(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.headers.get(CORRELATION_ID_HEADER)


async def test_an_inbound_correlation_id_is_echoed(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={CORRELATION_ID_HEADER: "trace-abc-123"})
    assert response.headers[CORRELATION_ID_HEADER] == "trace-abc-123"


async def test_a_hostile_inbound_id_is_replaced_not_reflected(client: AsyncClient) -> None:
    # A newline would let a caller forge extra log lines; it must be dropped and
    # a fresh id minted instead.
    response = await client.get("/healthz", headers={CORRELATION_ID_HEADER: "a\nb"})
    echoed = response.headers[CORRELATION_ID_HEADER]
    assert "\n" not in echoed
    assert echoed != "a\nb"
