"""The handler layer, tested without a database (SPEC §6.5: pure logic stays pure)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from webhook_receiver.domain.balance import CREDITED, DEBITED, SNAPSHOT, registry
from webhook_receiver.domain.effects import Credit, Effect, SetBalance
from webhook_receiver.domain.errors import (
    NonRetryableError,
    UnknownEventTypeError,
    UnprocessableEventError,
)
from webhook_receiver.domain.events import JsonObject, StoredEvent
from webhook_receiver.domain.handlers import HandlerRegistry

OCCURRED_AT = datetime(2026, 7, 11, 9, 0, 0, tzinfo=UTC)


def make_event(
    *,
    event_type: str = CREDITED,
    payload: JsonObject | None = None,
    entity_type: str = "account",
    entity_id: str = "acct_1",
    provider_sequence: int | None = 1,
) -> StoredEvent:
    return StoredEvent(
        id=1,
        source="stripe",
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        payload={"amount": 500} if payload is None else payload,
        occurred_at=OCCURRED_AT,
        provider_sequence=provider_sequence,
        attempt_count=0,
    )


class TestRegistry:
    def test_dispatches_to_the_registered_handler(self) -> None:
        effect = registry.dispatch(make_event(event_type=CREDITED, payload={"amount": 500}))

        assert effect == Credit(account_ref="acct_1", amount_minor=500)

    def test_unknown_event_type_raises_rather_than_being_skipped(self) -> None:
        # FR-8: an unregistered type is *recorded*, never quietly dropped.
        with pytest.raises(UnknownEventTypeError):
            registry.dispatch(make_event(event_type="invoice.exploded"))

    def test_an_unknown_event_type_is_not_retryable(self) -> None:
        # It will be just as unknown on the fifth attempt, so it must not consume
        # the retry budget -- it goes straight to the DLQ (FR-11, FR-14).
        assert issubclass(UnknownEventTypeError, NonRetryableError)

    def test_registering_the_same_event_type_twice_fails_at_import(self) -> None:
        # Two handlers for one type means the winner depends on import order.
        def handle(event: StoredEvent) -> Effect:
            return Credit(account_ref=event.entity_id, amount_minor=1)

        local = HandlerRegistry()
        local.register("a.b")(handle)

        with pytest.raises(ValueError, match="already registered"):
            local.register("a.b")(handle)

    def test_exposes_what_it_knows_about(self) -> None:
        assert registry.event_types == frozenset({CREDITED, DEBITED, SNAPSHOT})


class TestCreditAndDebit:
    def test_a_credit_is_positive(self) -> None:
        effect = registry.dispatch(make_event(event_type=CREDITED, payload={"amount": 250}))

        assert effect == Credit(account_ref="acct_1", amount_minor=250)

    def test_a_debit_is_the_same_effect_with_the_sign_flipped(self) -> None:
        # One effect type for both directions keeps `balance == SUM(ledger)` true
        # without any query having to remember to subtract.
        effect = registry.dispatch(make_event(event_type=DEBITED, payload={"amount": 250}))

        assert effect == Credit(account_ref="acct_1", amount_minor=-250)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({}, id="missing"),
            pytest.param({"amount": "500"}, id="a string, not a number"),
            pytest.param({"amount": 5.5}, id="a float -- money is never a float"),
            pytest.param({"amount": None}, id="null"),
            pytest.param({"amount": True}, id="a bool, which Python treats as 1"),
        ],
    )
    def test_an_amount_that_is_not_an_integer_is_unprocessable(self, payload: JsonObject) -> None:
        with pytest.raises(UnprocessableEventError):
            registry.dispatch(make_event(event_type=CREDITED, payload=payload))

    def test_a_negative_credit_is_refused(self) -> None:
        # A credit of -500 is a debit wearing a disguise. The event type carries
        # the direction; the amount must not contradict it.
        with pytest.raises(UnprocessableEventError):
            registry.dispatch(make_event(event_type=CREDITED, payload={"amount": -500}))

    def test_an_error_message_never_carries_the_payload_value(self) -> None:
        # NFR-6: this string lands in `last_error` and in the logs. It may name
        # the field; it may not name what was in it.
        with pytest.raises(UnprocessableEventError) as caught:
            registry.dispatch(make_event(payload={"amount": "4111111111111111"}))

        assert "4111111111111111" not in str(caught.value)

    def test_an_entity_this_handler_does_not_own_is_refused(self) -> None:
        with pytest.raises(UnprocessableEventError):
            registry.dispatch(make_event(entity_type="invoice"))

    def test_an_event_with_no_entity_id_is_refused(self) -> None:
        with pytest.raises(UnprocessableEventError):
            registry.dispatch(make_event(entity_id=""))


class TestSnapshot:
    def test_sets_an_absolute_balance(self) -> None:
        effect = registry.dispatch(
            make_event(event_type=SNAPSHOT, payload={"balance": 1200}, provider_sequence=7)
        )

        assert effect == SetBalance(account_ref="acct_1", balance_minor=1200, sequence=7)

    def test_a_snapshot_without_an_ordering_key_is_refused(self) -> None:
        # FR-10: a last-writer-wins effect with no way to tell who wrote last
        # cannot be applied safely. Refusing beats rewinding an account.
        with pytest.raises(UnprocessableEventError, match="provider_sequence"):
            registry.dispatch(
                make_event(event_type=SNAPSHOT, payload={"balance": 1200}, provider_sequence=None)
            )

    def test_a_negative_balance_is_allowed(self) -> None:
        # An account can genuinely be overdrawn. A snapshot reports what *is*.
        effect = registry.dispatch(
            make_event(event_type=SNAPSHOT, payload={"balance": -50}, provider_sequence=7)
        )

        assert effect == SetBalance(account_ref="acct_1", balance_minor=-50, sequence=7)

    def test_a_sequence_of_zero_is_refused(self) -> None:
        # 0 is the `version` of an account nothing has been applied to, and the
        # guard is a strict `>`, so a snapshot at 0 could never win. Refusing it
        # is honest; accepting it would be a snapshot that silently never applies.
        with pytest.raises(UnprocessableEventError, match="provider_sequence"):
            registry.dispatch(
                make_event(event_type=SNAPSHOT, payload={"balance": 10}, provider_sequence=0)
            )
