"""The advisory lock key derivation (FR-9).

The lock *behaviour* needs a real Postgres and lives in the integration suite.
What can be pinned here is the key itself -- and it needs pinning, because a key
that quietly changes between two workers is a bug that serialises nothing while
every test still passes.
"""

from __future__ import annotations

import pytest

from webhook_receiver.adapters.locks import advisory_lock_key

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def test_the_same_entity_always_derives_the_same_key() -> None:
    assert advisory_lock_key("account", "acct_1") == advisory_lock_key("account", "acct_1")


def test_the_key_is_pinned_to_a_literal() -> None:
    # Not a tautology: this is the test that fails if someone swaps the hash
    # function. Two workers deriving different keys for one account would take
    # different locks, serialise nothing, and corrupt a balance -- silently. A
    # rolling deploy is exactly when that happens, so the key is frozen here.
    assert advisory_lock_key("account", "acct_1") == 8_723_039_985_832_868_155


def test_different_entities_derive_different_keys() -> None:
    assert advisory_lock_key("account", "acct_1") != advisory_lock_key("account", "acct_2")


def test_the_entity_type_is_part_of_the_key() -> None:
    assert advisory_lock_key("account", "1") != advisory_lock_key("invoice", "1")


def test_the_separator_makes_the_encoding_unambiguous() -> None:
    # Without a separator, ("account", "1x") and ("account1", "x") concatenate to
    # the same string and would lock each other out.
    assert advisory_lock_key("account", "1x") != advisory_lock_key("account1", "x")


@pytest.mark.parametrize(
    "entity_id",
    ["acct_1", "", "acct_" + "9" * 500, "аккаунт", "acct/1?x=2", "\x00"],
)
def test_the_key_always_fits_a_postgres_bigint(entity_id: str) -> None:
    # pg_advisory_xact_lock takes a *signed* 64-bit integer. A key outside that
    # range is not a slow lock -- it is a NumericValueOutOfRange from the driver
    # and an event that can never be processed.
    key = advisory_lock_key("account", entity_id)

    assert INT64_MIN <= key <= INT64_MAX
