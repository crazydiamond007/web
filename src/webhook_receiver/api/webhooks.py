"""The ingestion endpoint: `POST /v1/webhooks/{source}` (FR-1..FR-5, NFR-3).

The pipeline is deliberately linear and short, because everything slow is
someone else's job (FR-2). This handler only:

    read raw bytes -> authenticate -> parse -> persist -> 200

Processing -- effects, retries, ordering -- happens later in the worker. The
one durability promise made here is NFR-3: the row is committed *before* the
`200`, so nothing we acknowledge can be lost to a crash.

Two failure modes, kept rigidly apart:

* **401** -- any authentication failure. An unknown source, a missing or
  malformed signature header, a stale timestamp, and a forged MAC are answered
  *identically*, on purpose. A distinguishable "unknown source" would tell an
  attacker which providers we integrate (see `signature.py`).
* **400** -- the request authenticated but its body is not an event we can
  route. This can only be reported *after* the signature check, so an attacker
  cannot use it as an oracle either.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from webhook_receiver.adapters.database import session_scope
from webhook_receiver.api.schemas import (
    IDEMPOTENCY_KEY_HEADER,
    IngestResponse,
    parse_envelope,
    redact_headers,
    resolve_idempotency_key,
    to_incoming_event,
)
from webhook_receiver.api.signature import SIGNATURE_HEADER, SignatureError, verify_signature
from webhook_receiver.api.state import AppStateDep, ClockDep
from webhook_receiver.domain.events import MalformedPayloadError
from webhook_receiver.obs import metrics
from webhook_receiver.services.ingest import ingest_event

router = APIRouter(prefix="/v1/webhooks", tags=["ingestion"])
log = structlog.get_logger(__name__)

# A generic 401 body. It carries no field an attacker can probe: whichever of the
# four authentication failures occurred, the answer is byte-identical.
_UNAUTHORIZED = JSONResponse(
    status_code=status.HTTP_401_UNAUTHORIZED,
    content={"detail": "signature verification failed"},
)


@router.post(
    "/{source}",
    status_code=status.HTTP_200_OK,
    response_model=IngestResponse,
    summary="Ingest a webhook delivery",
    responses={
        401: {"description": "Signature verification failed"},
        400: {"description": "Body is not a routable event"},
        413: {"description": "Body exceeds the configured size limit"},
    },
)
async def ingest(
    source: str,
    request: Request,
    state: AppStateDep,
    clock: ClockDep,
) -> Response:
    settings = state.settings
    structlog.contextvars.bind_contextvars(source=source)

    # FR-19 / NFR-2. Timed around the whole handler, including the commit: the
    # number that matters is what the *provider* waits for, not what we would like
    # to take credit for.
    with metrics.ingest_latency.labels(source=source).time():
        raw_body = await request.body()
        # Bound the work before doing any of it: hashing a body is linear in its
        # size, so an unbounded body is an unbounded CPU cost handed to an attacker.
        if len(raw_body) > settings.max_payload_bytes:
            log.warning("ingest.rejected", reason="payload_too_large", size=len(raw_body))
            metrics.events_rejected.labels(source=source, reason="payload_too_large").inc()
            return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)

        # An unknown source and a bad signature must be indistinguishable from
        # outside, so both return the byte-identical `_UNAUTHORIZED` below. Only
        # the internal log line -- never seen by the caller -- tells them apart.
        secret = settings.secret_for_source(source)
        if secret is None:
            log.warning("ingest.unauthorized", failure="UnknownSource")
            metrics.events_rejected.labels(source=source, reason="unauthorized").inc()
            return _UNAUTHORIZED

        try:
            verify_signature(
                secret=secret.get_secret_value(),
                raw_body=raw_body,
                raw_header=request.headers.get(SIGNATURE_HEADER),
                now=clock.now(),
                tolerance_seconds=settings.signature_timestamp_tolerance_seconds,
            )
        except SignatureError as exc:
            # The class name diagnoses it for us; it never reaches the client, and
            # it never carries the body or the secret (NFR-6).
            log.warning("ingest.unauthorized", failure=type(exc).__name__)
            # The *metric* is as coarse as the response. A per-failure-mode counter
            # would hand an attacker the oracle the 401 was carefully denying them,
            # via a /metrics endpoint that is easier to scrape than to time.
            metrics.events_rejected.labels(source=source, reason="unauthorized").inc()
            return _UNAUTHORIZED

        try:
            envelope = parse_envelope(raw_body)
            idempotency_key = resolve_idempotency_key(
                envelope, request.headers.get(IDEMPOTENCY_KEY_HEADER)
            )
        except MalformedPayloadError as exc:
            # Safe to surface: the request authenticated, so this is not an oracle,
            # and the message names offending *fields*, never their values.
            log.info("ingest.malformed", detail=str(exc))
            metrics.events_rejected.labels(source=source, reason="malformed").inc()
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)}
            )

        event = to_incoming_event(
            source=source,
            envelope=envelope,
            payload=envelope.data,
            headers=redact_headers(dict(request.headers)),
            idempotency_key=idempotency_key,
            signature_verified=True,
        )

        async with session_scope(state.session_factory) as session:
            result = await ingest_event(session, event)

    metrics.events_ingested.labels(source=source, duplicate=str(result.duplicate).lower()).inc()

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=IngestResponse(
            status="accepted",
            event_id=result.event_id,
            duplicate=result.duplicate,
        ).model_dump(),
    )
