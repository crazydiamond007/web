#!/usr/bin/env python
"""POST a correctly signed webhook to a running instance -- for manual testing.

It signs with the *same* secret the server verifies against, by reading it out of
`Settings` (your `.env`), so a delivery it produces always authenticates. Send
the same event twice to watch idempotency work:

    make send ARGS="--count 2"     # one row, two 200s, the second a duplicate

Stdlib only (urllib), so it runs without the test dependencies. It signs and
sends; it applies no effects -- that is the worker's job.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from uuid import uuid4

from webhook_receiver.api.signature import SIGNATURE_HEADER, expected_signature
from webhook_receiver.config import get_settings


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a signed test webhook.")
    parser.add_argument("--url", default="http://localhost:8000", help="Base URL of the app.")
    parser.add_argument("--source", default="stripe", help="The {source} path segment.")
    parser.add_argument("--event-id", default=None, help="Event id (default: time-based).")
    parser.add_argument(
        "--event-type",
        default="balance.credited",
        help="balance.credited | balance.debited | balance.snapshot.",
    )
    parser.add_argument("--entity-type", default="account", help="Entity type.")
    parser.add_argument("--entity-id", default="acct_1", help="Entity id.")
    parser.add_argument("--amount", type=int, default=500, help="Minor units to credit or debit.")
    parser.add_argument(
        "--balance",
        type=int,
        default=1000,
        help="Absolute balance, for balance.snapshot only.",
    )
    parser.add_argument(
        "--sequence",
        type=int,
        default=None,
        help="Provider ordering key (FR-10). Required by balance.snapshot; send a "
        "lower one after a higher one to watch a stale event be superseded.",
    )
    parser.add_argument("--idempotency-key", default=None, help="Override the dedup key.")
    parser.add_argument("--count", type=int, default=1, help="Send the same event N times.")
    parser.add_argument(
        "--skew",
        type=int,
        default=0,
        help="Add this many seconds to the signed timestamp (try 400 to force a 401).",
    )
    return parser.parse_args()


SNAPSHOT = "balance.snapshot"


def _build_body(args: argparse.Namespace, event_id: str) -> bytes:
    # A snapshot asserts an absolute balance; a credit or debit moves it. The
    # worker's handlers read different fields for the two, so the sender has to
    # send the right one.
    data = {"balance": args.balance} if args.event_type == SNAPSHOT else {"amount": args.amount}

    envelope: dict[str, object] = {
        "id": event_id,
        "type": args.event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
        "entity": {"type": args.entity_type, "id": args.entity_id},
        "data": data,
    }
    if args.sequence is not None:
        envelope["sequence"] = args.sequence

    # Sort keys so the bytes are stable -- the signature covers exactly these.
    return json.dumps(envelope, sort_keys=True).encode("utf-8")


def _send(args: argparse.Namespace, secret: str, event_id: str) -> int:
    body = _build_body(args, event_id)
    timestamp = int(time.time()) + args.skew
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: f"t={timestamp},v1={expected_signature(secret, timestamp, body)}",
    }
    if args.idempotency_key is not None:
        headers["Idempotency-Key"] = args.idempotency_key

    request = urllib.request.Request(  # noqa: S310 (fixed localhost URL, not user-driven)
        f"{args.url}/v1/webhooks/{args.source}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310
            print(f"  {response.status} {response.read().decode()}")
            return int(response.status)
    except urllib.error.HTTPError as exc:
        # A 4xx is an expected outcome here (e.g. --skew to demo a stale 401),
        # not a script failure, so report it rather than raising.
        print(f"  {exc.code} {exc.read().decode()}")
        return int(exc.code)
    except urllib.error.URLError as exc:
        # Nothing is listening. A forty-line traceback for "the app isn't running"
        # tells you less than one line does, and this is a script whose whole job
        # is to be run by a human at a terminal.
        print(f"  cannot reach {args.url}: {exc.reason}", file=sys.stderr)
        print("  is the stack up? try `make up`", file=sys.stderr)
        return 1


def main() -> int:
    args = _parse_args()

    secret_value = get_settings().secret_for_source(args.source)
    if secret_value is None:
        print(
            f"no secret configured for source {args.source!r}; add it to WEBHOOK_SECRETS in .env",
            file=sys.stderr,
        )
        return 1
    secret = secret_value.get_secret_value()

    # A shared event id so repeated sends within ONE invocation are the *same*
    # event -- that is what exercises the dedup constraint (FR-5).
    #
    # Random, not `int(time.time())`: a time-based id makes two invocations inside
    # the same second collide, and the receiver then does exactly what it is built
    # to do -- treats the second, unrelated event as a redelivery of the first and
    # discards it. Correct behaviour, wrong events, and a demo that appears to
    # prove the opposite of what it claims.
    event_id = args.event_id or f"evt_{uuid4().hex[:12]}"
    print(f"POST {args.url}/v1/webhooks/{args.source}  (event id: {event_id})")
    for i in range(1, args.count + 1):
        print(f"delivery {i}/{args.count}:")
        _send(args, secret, event_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
