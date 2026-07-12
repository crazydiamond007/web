"""Which failures earn a retry (FR-11, SPEC §6.6).

The default is the point of this file. SPEC §6.6: "anything unclassified is
treated as non-retryable and dead-lettered." So the tests are written to catch the
two ways that rule can be got wrong:

* retrying something we do not understand (a bug, hammered five times), and
* dead-lettering a database failover (a blip, turned into a manual replay of
  every event in flight).
"""

from __future__ import annotations

import socket

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from webhook_receiver.adapters.failures import RETRYABLE_SQLSTATES, is_retryable
from webhook_receiver.domain.errors import (
    LockContentionError,
    NonRetryableError,
    RetryableError,
    UnknownEventTypeError,
    UnprocessableEventError,
)


class FakePgError(Exception):
    """Stands in for an asyncpg error, which carries a `sqlstate`."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"postgres said {sqlstate}")
        self.sqlstate = sqlstate


def dbapi_error(sqlstate: str, *, invalidated: bool = False) -> DBAPIError:
    exc = OperationalError("SELECT 1", {}, FakePgError(sqlstate))
    exc.connection_invalidated = invalidated
    return exc


class TestTheDomainTaxonomyWins:
    def test_a_domain_retryable_error_is_retryable(self) -> None:
        assert is_retryable(LockContentionError("busy")) is True

    @pytest.mark.parametrize(
        "exc",
        [
            UnknownEventTypeError("no handler"),
            UnprocessableEventError("bad field"),
        ],
    )
    def test_a_domain_non_retryable_error_is_not(self, exc: NonRetryableError) -> None:
        # FR-11's acceptance: straight to the DLQ without burning retries.
        assert is_retryable(exc) is False

    def test_the_base_classes_carry_the_decision(self) -> None:
        # A new error added later inherits the right answer by choosing its base
        # class, rather than by remembering to update a lookup table here.
        class NewTransientError(RetryableError):
            pass

        class NewPermanentError(NonRetryableError):
            pass

        assert is_retryable(NewTransientError()) is True
        assert is_retryable(NewPermanentError()) is False


class TestInfrastructureIsRecognised:
    """The enumerated transient cases -- without these the §6.6 default is unsafe."""

    @pytest.mark.parametrize("sqlstate", sorted(RETRYABLE_SQLSTATES))
    def test_transient_postgres_states_are_retryable(self, sqlstate: str) -> None:
        assert is_retryable(dbapi_error(sqlstate)) is True

    def test_a_failover_does_not_dead_letter_the_event(self) -> None:
        # 57P01 is admin_shutdown -- an RDS failover, or someone restarting the
        # server. If this were treated as unclassified, a fifteen-second blip
        # would dead-letter every event in flight and demand a manual replay.
        assert is_retryable(dbapi_error("57P01")) is True

    def test_an_invalidated_connection_is_retryable_whatever_the_sqlstate(self) -> None:
        # The connection died under the statement, so the statement never ran.
        # Running it again is exactly right.
        assert is_retryable(dbapi_error("XX000", invalidated=True)) is True

    def test_a_pool_timeout_is_retryable(self) -> None:
        # Nothing to do with the event; everything to do with load right now.
        assert is_retryable(SQLAlchemyTimeoutError()) is True

    @pytest.mark.parametrize("exc", [ConnectionResetError(), TimeoutError()])
    def test_socket_level_failures_are_retryable(self, exc: OSError) -> None:
        assert is_retryable(exc) is True


class TestUnclassifiedIsNotRetried:
    """SPEC §6.6: what we cannot classify, we do not retry."""

    @pytest.mark.parametrize(
        "exc",
        [
            TypeError("NoneType is not subscriptable"),
            KeyError("amount"),
            ValueError("invalid literal"),
            AttributeError("'NoneType' object has no attribute 'id'"),
        ],
    )
    def test_a_bug_in_our_code_goes_straight_to_the_dlq(self, exc: Exception) -> None:
        # These are all bugs. A bug is not fixed by a fourth attempt: retrying
        # wastes the budget, delays every event behind it, and buries the stack
        # trace under four identical copies of itself.
        assert is_retryable(exc) is False

    def test_a_constraint_violation_is_not_retryable(self) -> None:
        # 23505 is unique_violation. Our *intended* conflicts are handled by
        # ON CONFLICT and never raise; one that reaches here is a real schema or
        # logic bug, and it will violate the constraint again next time.
        exc = IntegrityError("INSERT", {}, FakePgError("23505"))

        assert is_retryable(exc) is False

    def test_a_syntax_error_in_our_sql_is_not_retryable(self) -> None:
        # 42601 is syntax_error. It will be just as wrong on the fifth attempt.
        assert is_retryable(dbapi_error("42601")) is False

    def test_a_database_error_with_no_sqlstate_is_not_retryable(self) -> None:
        exc = OperationalError("SELECT 1", {}, Exception("something odd"))

        assert is_retryable(exc) is False


class TestSocketLevelFailures:
    """asyncpg can fail *before* SQLAlchemy has anything to wrap."""

    def test_a_dns_failure_is_retryable(self) -> None:
        # What asyncpg actually raises -- raw and unwrapped -- when the database's
        # hostname does not resolve: a managed Postgres cycling its container, or a
        # private-network blip. gaierror is an OSError but NOT a ConnectionError,
        # so it slips past the obvious check; unclassified, a brief DNS wobble
        # would dead-letter every event in flight.
        assert is_retryable(socket.gaierror(-2, "Name or service not known")) is True

    def test_a_refused_connection_is_retryable(self) -> None:
        assert is_retryable(ConnectionRefusedError("connection refused")) is True

    def test_an_unrelated_oserror_is_not_retryable(self) -> None:
        # The net stays narrow on purpose. Widening it to OSError would sweep in
        # PermissionError and FileNotFoundError -- our bugs, not the world's.
        assert is_retryable(PermissionError("denied")) is False
