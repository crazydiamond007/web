"""Admin authentication (FR-20).

An API key in a header, compared in constant time. Deliberately *not* the same
mechanism as ingestion, because the two are answering different questions:

* **Ingestion** asks "did the provider send this?" -- so it verifies a signature
  over the body, and it has to, because the provider has no account here and the
  body is the thing that must not have been tampered with (FR-3).
* **Admin** asks "is this us?" -- an operator replaying events or draining the
  DLQ. There is no body to authenticate and no third party to authenticate to.

Constant-time comparison, as in `signature.py`: `==` on a secret short-circuits at
the first differing byte, and the timing difference is measurable across a network
given enough samples. It is the cheapest possible mitigation and there is no reason
not to.
"""

from __future__ import annotations

import hmac
from typing import Annotated

import structlog
from fastapi import Depends, Header, HTTPException, status

from webhook_receiver.api.state import AppStateDep

log = structlog.get_logger(__name__)

ADMIN_KEY_HEADER = "X-Admin-Key"

# A 401 with no detail about *why*. An admin endpoint is a smaller target than
# ingestion, but the reasoning is the same: "wrong key" and "no key" must look
# identical, or the response becomes an oracle.
_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="admin authentication failed",
    headers={"WWW-Authenticate": "X-Admin-Key"},
)


async def require_admin(
    state: AppStateDep,
    x_admin_key: Annotated[str | None, Header(alias=ADMIN_KEY_HEADER)] = None,
) -> str:
    """Guard every admin route. Returns the operator identity for the audit trail.

    The identity is currently just "the holder of the key" -- a shared key cannot
    tell two operators apart. `replay_request.requested_by` records it anyway,
    because the *shape* of the audit is what matters: when this is swapped for
    real per-operator credentials (OIDC, mTLS), nothing downstream changes.
    """
    expected = state.settings.admin_api_key.get_secret_value()

    if x_admin_key is None:
        log.warning("admin.unauthorized", reason="missing_key")
        raise _UNAUTHORIZED

    if not hmac.compare_digest(x_admin_key, expected):
        log.warning("admin.unauthorized", reason="bad_key")
        raise _UNAUTHORIZED

    return "api-key"


AdminDep = Annotated[str, Depends(require_admin)]
