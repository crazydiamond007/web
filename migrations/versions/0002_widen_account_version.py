"""widen account.version to bigint

`account.version` is the optimistic-ordering guard for FR-10: it holds the
`provider_sequence` of the newest event applied to the account, and a snapshot
whose sequence is not greater than it has been superseded.

That makes it a *store of a provider sequence*, and `webhook_event.provider_sequence`
is `bigint`. As `integer` it silently truncates above 2^31-1 -- and a provider
that numbers its events from a global counter (Stripe, Shopify) reaches that
scale. The failure would not be a clean error: the comparison would be made
against a wrong number, and a live snapshot would be discarded as stale.

Widening `integer` -> `bigint` rewrites the table on Postgres, which is fine for
`account` here and worth flagging as a lock-taking migration on a large one.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "account",
        "version",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )


def downgrade() -> None:
    # Narrowing is lossy by definition: any row already past 2^31-1 cannot fit.
    # Postgres refuses the cast rather than truncating, so the downgrade fails
    # loudly instead of corrupting the guard. That is the correct behaviour --
    # there is nothing safe to do with a value that does not fit.
    op.alter_column(
        "account",
        "version",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
