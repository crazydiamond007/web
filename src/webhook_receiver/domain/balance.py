"""The demo domain: money moving into and out of an account (FR-8).

Deliberately a balance rather than something inert, because a balance makes
double-processing *visible*. If the same delivery is applied twice, the number is
wrong, and no amount of confident prose can hide it. That is the whole point of
SPEC §3's "note on the demo domain".

Three event types, chosen to cover both halves of the correctness problem:

* ``balance.credited`` / ``balance.debited`` -- additive, commutative. These
  exercise **exactly-once** (FR-6): applied twice, the balance doubles.
* ``balance.snapshot`` -- an absolute balance the provider reconciles us to.
  Last-writer-wins, so it exercises **ordering** (FR-10): applied out of order,
  the balance silently rewinds to a stale value.

Everything here is a pure function of the event. No session, no clock, no I/O.
"""

from __future__ import annotations

from webhook_receiver.domain.effects import Credit, SetBalance
from webhook_receiver.domain.errors import UnprocessableEventError
from webhook_receiver.domain.events import JsonObject, StoredEvent
from webhook_receiver.domain.handlers import HandlerRegistry

ACCOUNT_ENTITY_TYPE = "account"

CREDITED = "balance.credited"
DEBITED = "balance.debited"
SNAPSHOT = "balance.snapshot"

# The lowest ordering key a snapshot may carry. 0 is reserved for "no event has
# been applied to this account yet" -- it is the `account.version` default -- and
# the ordering guard is a strict `>`, so a snapshot at 0 could never win.
MIN_SEQUENCE = 1


def _account_ref(event: StoredEvent) -> str:
    """The account this event is about, or a refusal to guess."""
    if event.entity_type != ACCOUNT_ENTITY_TYPE:
        msg = (
            f"balance handlers own {ACCOUNT_ENTITY_TYPE!r} entities, "
            f"but this event is about {event.entity_type!r}"
        )
        raise UnprocessableEventError(msg)
    if not event.entity_id:
        msg = "event carries no entity_id, so there is no account to apply it to"
        raise UnprocessableEventError(msg)
    return event.entity_id


def _minor_units(payload: JsonObject, field: str) -> int:
    """Read an integer money field, in minor units (cents), or refuse.

    Money is never a float. `0.1 + 0.2 != 0.3` in binary floating point, and an
    accumulating rounding error in a ledger is a defect you find in an audit
    rather than in a test.

    `bool` is excluded explicitly because Python makes `True` an `int`, so
    `isinstance(True, int)` is `True` and a payload of `{"amount": true}` would
    otherwise credit one cent.

    The message names the *field*, never its value: this string reaches the log
    and the `last_error` column, and payloads never do (NFR-6).
    """
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"field {field!r} must be an integer number of minor units"
        raise UnprocessableEventError(msg)
    return value


def _non_negative(amount: int, field: str) -> int:
    """A credit of -500 is a debit wearing a disguise. Refuse it.

    The direction of the money is carried by the *event type*, not by the sign of
    the amount. Allowing both would mean two ways to express the same movement,
    and the day they disagree is the day the ledger stops adding up.
    """
    if amount < 0:
        msg = f"field {field!r} must not be negative; the event type carries the direction"
        raise UnprocessableEventError(msg)
    return amount


registry = HandlerRegistry()


@registry.register(CREDITED)
def handle_credited(event: StoredEvent) -> Credit:
    amount = _non_negative(_minor_units(event.payload, "amount"), "amount")
    return Credit(account_ref=_account_ref(event), amount_minor=amount)


@registry.register(DEBITED)
def handle_debited(event: StoredEvent) -> Credit:
    amount = _non_negative(_minor_units(event.payload, "amount"), "amount")
    # One effect type for both directions: a debit is a negative credit, so the
    # ledger stays a plain sum and `balance == SUM(amount_minor)` holds by
    # construction rather than by a query that remembers to subtract.
    return Credit(account_ref=_account_ref(event), amount_minor=-amount)


@registry.register(SNAPSHOT)
def handle_snapshot(event: StoredEvent) -> SetBalance:
    """Reconcile to an absolute balance -- the one event type that can go stale.

    It is refused outright without a usable `provider_sequence`. A last-writer-
    wins effect with no way to tell who wrote last is not something we can apply
    safely, and applying it anyway would mean a late redelivery could rewind an
    account to a balance that stopped being true hours ago. Refusing is the
    conservative failure: the event is dead-lettered and a human sees it, rather
    than the balance quietly going wrong.

    The sequence must be `>= 1`. Zero is reserved: it is the `version` of an
    account nothing has been applied to yet, and the guard is a strict `>`.
    """
    sequence = event.provider_sequence
    if sequence is None or sequence < MIN_SEQUENCE:
        msg = (
            f"{SNAPSHOT!r} sets an absolute balance and needs a provider_sequence "
            f"of at least {MIN_SEQUENCE} to order it; without one a stale snapshot "
            f"could overwrite newer state"
        )
        raise UnprocessableEventError(msg)
    # Not `_non_negative`: an account genuinely can be overdrawn, and a snapshot
    # reports what *is*, not what should be.
    balance = _minor_units(event.payload, "balance")
    return SetBalance(
        account_ref=_account_ref(event),
        balance_minor=balance,
        sequence=sequence,
    )
