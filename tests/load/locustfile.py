"""The load test that turns NFR-1 from an argument into a number.

    locust -f tests/load/locustfile.py --headless -u 50 -r 25 -t 60s \
           --host http://localhost:8000

or, with the assertions and the report, `make load`.

**What this is actually testing.** Not throughput -- throughput is a by-product.
It is testing that under real concurrency, with real duplicates, hitting a small
pool of accounts hard enough that workers *must* collide on them:

    count(ledger_entry) == count(distinct events)

Every delivery is sent **twice**, back to back, because that is what a provider
does when our 200 does not reach it in time. So roughly half of all traffic here
is a redelivery of something already stored, which is the exact condition the
whole service exists to survive.

The accounts are deliberately few (`LOAD_ACCOUNTS`, default 50). Spreading
10,000 events over 10,000 accounts would prove nothing: nothing would ever
contend, the advisory locks would never be taken under pressure, and the test
would pass on a system with no locking at all. Concentrating them is the point --
it forces `pg_advisory_xact_lock` to actually serialise, and forces
`FOR UPDATE SKIP LOCKED` to actually arbitrate between workers.

Event ids come from a fixed pool (`load_0` .. `load_N-1`) rather than from
uuid4(). Two reasons: collisions across users produce *additional* natural
duplicates, and -- more importantly -- the pool is derivable without coordination,
so this still works under `--processes` where locust workers share no memory.

`scripts/verify_load.py` does the asserting. It reads the database, not this
process: a load test that grades its own homework is not evidence.
"""

from __future__ import annotations

import json
import os
import random
import time
from typing import Any

from locust import HttpUser, between, task

from webhook_receiver.api.signature import SIGNATURE_HEADER, expected_signature
from webhook_receiver.config import get_settings

SOURCE = os.environ.get("LOAD_SOURCE", "stripe")

# The pool of distinct events. Every one of them will be delivered at least twice.
POOL_SIZE = int(os.environ.get("LOAD_POOL_SIZE", "2500"))

# Few enough that workers are forced to contend on them. This is the setting that
# decides whether the test means anything.
ACCOUNTS = int(os.environ.get("LOAD_ACCOUNTS", "50"))

# Every event credits the same amount, so the expected balance of an account is
# simply `AMOUNT * (number of distinct events for it)` -- a figure the verifier can
# derive from the database without trusting anything this process reports.
AMOUNT = int(os.environ.get("LOAD_AMOUNT", "100"))


def _secret() -> str:
    secret = get_settings().secret_for_source(SOURCE)
    if secret is None:
        msg = f"no secret configured for source {SOURCE!r}; set WEBHOOK_SECRETS in .env"
        raise RuntimeError(msg)
    return secret.get_secret_value()


class WebhookProvider(HttpUser):
    """One provider, delivering events and redelivering the ones it doubts."""

    # A little think time: a provider is not a tight loop, and pinning the CPU on
    # the load generator would measure Python, not the service.
    wait_time = between(0.0, 0.05)

    def on_start(self) -> None:
        self.secret = _secret()

    def _body(self, index: int) -> bytes:
        envelope: dict[str, Any] = {
            "id": f"load_{index}",
            "type": "balance.credited",
            "occurred_at": "2026-07-11T12:00:00Z",
            "entity": {"type": "account", "id": f"load_acct_{index % ACCOUNTS}"},
            "data": {"amount": AMOUNT},
        }
        return json.dumps(envelope, sort_keys=True).encode()

    def _deliver(self, body: bytes, *, name: str) -> None:
        timestamp = int(time.time())
        self.client.post(
            f"/v1/webhooks/{SOURCE}",
            data=body,
            headers={
                "Content-Type": "application/json",
                SIGNATURE_HEADER: (
                    f"t={timestamp},v1={expected_signature(self.secret, timestamp, body)}"
                ),
            },
            name=name,
        )

    @task
    def deliver_then_redeliver(self) -> None:
        """One event, delivered twice -- exactly what an at-least-once provider does.

        The two are reported under separate names so the run distinguishes a first
        delivery from a redelivery. They must be *indistinguishable in latency*:
        if the duplicate path were slower, a provider hammering us with retries
        during an incident would make the incident worse.
        """
        # `random`, not `secrets`: this is load shaping, not cryptography.
        body = self._body(random.randrange(POOL_SIZE))

        self._deliver(body, name="POST /v1/webhooks/{source} [first]")
        self._deliver(body, name="POST /v1/webhooks/{source} [redelivery]")
