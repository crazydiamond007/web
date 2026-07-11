"""The admin API, end to end (FR-15, FR-16, FR-18, FR-19, FR-20).

FR-20's acceptance is the first thing tested and the most important: an
unauthenticated admin call is a `401`. Everything else in this file is only worth
having if that holds.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from webhook_receiver.adapters.orm import LedgerEntry, WebhookEvent
from webhook_receiver.api.app import create_app
from webhook_receiver.api.auth import ADMIN_KEY_HEADER
from webhook_receiver.config import Settings
from webhook_receiver.domain.balance import CREDITED
from webhook_receiver.domain.enums import WebhookStatus

pytestmark = pytest.mark.integration

ADMIN_KEY = "an-admin-key-long-enough-to-be-real"
NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)

ADMIN_ROUTES = [
    ("GET", "/v1/admin/events"),
    ("GET", "/v1/admin/events/1"),
    ("GET", "/v1/admin/dlq"),
    ("POST", "/v1/admin/dlq/1/resolve"),
    ("POST", "/v1/admin/replay"),
]


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        admin_api_key=ADMIN_KEY,
        webhook_secrets={"stripe": "whsec_integration"},
        _env_file=None,
    )


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(str(settings.database_url))
    yield eng
    await eng.dispose()


@pytest.fixture
async def client(settings: Settings, migrated_schema: None) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
        app.router.lifespan_context(app),
    ):
        yield http


def auth() -> dict[str, str]:
    return {ADMIN_KEY_HEADER: ADMIN_KEY}


async def seed_event(
    engine: AsyncEngine,
    *,
    external_id: str = "evt_1",
    status: WebhookStatus = WebhookStatus.PENDING,
    entity_id: str = "acct_1",
) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(
            sa.insert(WebhookEvent)
            .values(
                source="stripe",
                external_id=external_id,
                idempotency_key=external_id,
                event_type=CREDITED,
                entity_type="account",
                entity_id=entity_id,
                payload={"amount": 500, "card": "4111111111111111"},
                headers={"x-secret": "shhh"},
                signature_verified=True,
                occurred_at=NOW,
                next_attempt_at=NOW,
                status=status,
            )
            .returning(WebhookEvent.id)
        )
        return int(result.scalar_one())


class TestAuth:
    """FR-20."""

    @pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
    async def test_an_unauthenticated_admin_call_is_rejected(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        response = await client.request(method, path, json={})

        assert response.status_code == 401

    @pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
    async def test_a_wrong_key_is_rejected(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        response = await client.request(
            method, path, json={}, headers={ADMIN_KEY_HEADER: "not-the-key"}
        )

        assert response.status_code == 401

    async def test_a_missing_key_and_a_wrong_key_look_identical(self, client: AsyncClient) -> None:
        # No oracle. "Wrong key" and "no key" must not be distinguishable, or the
        # response tells an attacker whether they are close.
        missing = await client.get("/v1/admin/events")
        wrong = await client.get("/v1/admin/events", headers={ADMIN_KEY_HEADER: "nope"})

        assert missing.status_code == wrong.status_code == 401
        assert missing.json() == wrong.json()

    async def test_ingestion_is_not_behind_the_admin_key(self, client: AsyncClient) -> None:
        # A provider has no API key. Ingestion authenticates by signature, and the
        # two mechanisms must not be confused for one another (FR-3 vs FR-20).
        response = await client.post("/v1/webhooks/stripe", content=b"{}")

        assert response.status_code == 401  # a signature failure, not an admin one
        assert response.json()["detail"] == "signature verification failed"


class TestEventQuery:
    """FR-18."""

    async def test_lists_events_and_filters_them(
        self, client: AsyncClient, engine: AsyncEngine
    ) -> None:
        await seed_event(engine, external_id="evt_a", entity_id="acct_1")
        await seed_event(
            engine, external_id="evt_b", entity_id="acct_2", status=WebhookStatus.SUCCEEDED
        )

        everything = await client.get("/v1/admin/events", headers=auth())
        by_status = await client.get(
            "/v1/admin/events", params={"status": "succeeded"}, headers=auth()
        )
        by_entity = await client.get(
            "/v1/admin/events", params={"entity_id": "acct_1"}, headers=auth()
        )

        assert len(everything.json()) == 2
        assert [e["external_id"] for e in by_status.json()] == ["evt_b"]
        assert [e["external_id"] for e in by_entity.json()] == ["evt_a"]

    async def test_never_returns_the_payload_or_the_headers(
        self, client: AsyncClient, engine: AsyncEngine
    ) -> None:
        # NFR-6. This is a support tool, and a support tool that prints the body
        # turns every screenshot pasted into a ticket into a leak. The seeded event
        # carries a card number and a secret header; neither may come back.
        event_id = await seed_event(engine)

        listing = await client.get("/v1/admin/events", headers=auth())
        detail = await client.get(f"/v1/admin/events/{event_id}", headers=auth())

        for body in (listing.text, detail.text):
            assert "4111111111111111" not in body
            assert "shhh" not in body
            assert "payload" not in body
            assert "headers" not in body

    async def test_a_single_event_carries_its_attempt_history(
        self, client: AsyncClient, engine: AsyncEngine
    ) -> None:
        # The history is the point: "three retryable failures with a growing
        # backoff" is a very different story from "one non-retryable failure", and
        # neither is visible from the event row alone.
        event_id = await seed_event(engine)

        response = await client.get(f"/v1/admin/events/{event_id}", headers=auth())

        assert response.status_code == 200
        assert response.json()["attempts"] == []  # nothing has processed it yet

    async def test_an_unknown_event_is_a_404(self, client: AsyncClient) -> None:
        response = await client.get("/v1/admin/events/9999", headers=auth())

        assert response.status_code == 404


class TestDlqTriage:
    """FR-15."""

    async def _dead_letter(self, client: AsyncClient, engine: AsyncEngine) -> int:
        """Produce a real DLQ entry by replaying an event with no handler."""
        event_id = await seed_event(engine, external_id="evt_poison")
        async with engine.begin() as conn:
            await conn.execute(
                sa.update(WebhookEvent)
                .where(WebhookEvent.id == event_id)
                .values(event_type="nothing.handles.this")
            )
        await client.post("/v1/admin/replay", json={"event_ids": [event_id]}, headers=auth())

        entries = (await client.get("/v1/admin/dlq", headers=auth())).json()
        assert len(entries) == 1
        return int(entries[0]["id"])

    async def test_an_unhandled_event_lands_in_the_dlq(
        self, client: AsyncClient, engine: AsyncEngine
    ) -> None:
        entry_id = await self._dead_letter(client, engine)

        entries = (await client.get("/v1/admin/dlq", headers=auth())).json()

        assert entries[0]["id"] == entry_id
        assert entries[0]["status"] == "needs_review"
        assert entries[0]["attempts_made"] == 1  # non-retryable: no budget burned
        assert "UnknownEventTypeError" in entries[0]["reason"]

    async def test_an_operator_can_resolve_an_entry(
        self, client: AsyncClient, engine: AsyncEngine
    ) -> None:
        entry_id = await self._dead_letter(client, engine)

        response = await client.post(
            f"/v1/admin/dlq/{entry_id}/resolve",
            json={"note": "handler shipped in v1.2"},
            headers=auth(),
        )

        assert response.status_code == 200
        assert response.json()["status"] == "resolved"
        assert response.json()["resolution_note"] == "handler shipped in v1.2"
        assert response.json()["resolved_at"] is not None

    async def test_an_operator_can_discard_an_entry(
        self, client: AsyncClient, engine: AsyncEngine
    ) -> None:
        entry_id = await self._dead_letter(client, engine)

        response = await client.post(
            f"/v1/admin/dlq/{entry_id}/discard", json={"note": "test traffic"}, headers=auth()
        )

        assert response.json()["status"] == "discarded"

    async def test_a_resolved_entry_does_not_quietly_reopen(
        self, client: AsyncClient, engine: AsyncEngine
    ) -> None:
        # `resolved` and `discarded` are terminal. An entry a human has ruled on
        # stays ruled on: a second decision would silently overwrite the first and
        # make the history lie about what happened when.
        entry_id = await self._dead_letter(client, engine)
        await client.post(f"/v1/admin/dlq/{entry_id}/resolve", json={}, headers=auth())

        second = await client.post(f"/v1/admin/dlq/{entry_id}/discard", json={}, headers=auth())

        assert second.status_code == 409  # not 400: the request is fine, the state refuses
        assert "terminal" in second.json()["detail"]

    async def test_triaging_an_unknown_entry_is_a_404(self, client: AsyncClient) -> None:
        response = await client.post("/v1/admin/dlq/9999/resolve", json={}, headers=auth())

        assert response.status_code == 404


class TestReplayApi:
    """FR-16, FR-17."""

    async def test_replaying_a_processed_event_adds_no_effect(
        self, client: AsyncClient, engine: AsyncEngine
    ) -> None:
        event_id = await seed_event(engine)

        first = await client.post(
            "/v1/admin/replay",
            json={"event_ids": [event_id], "reason": "ticket 1"},
            headers=auth(),
        )
        second = await client.post(
            "/v1/admin/replay",
            json={"event_ids": [event_id], "reason": "ticket 1 again"},
            headers=auth(),
        )

        assert first.json()["results"][0]["outcome"] == "succeeded"
        # FR-17: the ledger's unique constraint refused a second effect.
        assert second.json()["results"][0]["outcome"] == "skipped_already_processed"

        async with engine.connect() as conn:
            ledger = await conn.execute(sa.select(sa.func.count()).select_from(LedgerEntry))
            balance = await conn.execute(sa.text("SELECT balance_minor FROM account"))

        assert ledger.scalar_one() == 1
        assert balance.scalar_one() == 500  # the money did not move twice

    async def test_a_replay_needs_exactly_one_selector(self, client: AsyncClient) -> None:
        both = await client.post(
            "/v1/admin/replay", json={"event_ids": [1], "dead_lettered": True}, headers=auth()
        )
        neither = await client.post("/v1/admin/replay", json={}, headers=auth())

        assert both.status_code == 422
        assert neither.status_code == 422

    async def test_a_replay_batch_is_bounded(
        self, client: AsyncClient, engine: AsyncEngine
    ) -> None:
        # Replay is synchronous and takes an advisory lock per event. An unbounded
        # "replay everything" is a self-inflicted outage wearing a recovery's
        # clothes, so the bound is enforced rather than documented.
        too_many = list(range(1, 202))

        response = await client.post(
            "/v1/admin/replay", json={"event_ids": too_many}, headers=auth()
        )

        assert response.status_code == 400
        assert "the limit is 100" in response.json()["detail"]

    async def test_replaying_an_unknown_event_is_a_404(self, client: AsyncClient) -> None:
        response = await client.post("/v1/admin/replay", json={"event_ids": [9999]}, headers=auth())

        assert response.status_code == 404

    async def test_the_dlq_can_be_drained_in_one_call(
        self, client: AsyncClient, engine: AsyncEngine
    ) -> None:
        event_id = await seed_event(engine, external_id="evt_poison")
        async with engine.begin() as conn:
            await conn.execute(
                sa.update(WebhookEvent)
                .where(WebhookEvent.id == event_id)
                .values(event_type="nothing.handles.this")
            )
        await client.post("/v1/admin/replay", json={"event_ids": [event_id]}, headers=auth())

        # The handler still does not exist, so draining the DLQ fails it again --
        # and the entry goes back to needs_review rather than being stranded.
        response = await client.post(
            "/v1/admin/replay", json={"dead_lettered": True, "reason": "drain"}, headers=auth()
        )

        assert response.json()["requested"] == 1
        assert response.json()["results"][0]["outcome"] == "failed"

        entries = (await client.get("/v1/admin/dlq", headers=auth())).json()
        assert entries[0]["status"] == "needs_review"


class TestMetrics:
    """FR-19."""

    async def test_metrics_are_exposed(self, client: AsyncClient) -> None:
        response = await client.get("/metrics")

        assert response.status_code == 200
        assert "webhook_events_ingested_total" in response.text

    async def test_metrics_never_carry_a_payload_or_an_entity_id(
        self, client: AsyncClient, engine: AsyncEngine
    ) -> None:
        # An unbounded label is not a metric, it is a memory leak with a dashboard:
        # Prometheus keeps a time series per label combination, forever.
        event_id = await seed_event(engine)
        await client.post("/v1/admin/replay", json={"event_ids": [event_id]}, headers=auth())

        body = (await client.get("/metrics")).text

        assert "acct_1" not in body
        assert "4111111111111111" not in body
