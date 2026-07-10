"""Correlation id, bound once per request (NFR-5).

Every log line emitted while handling a request carries the same
``correlation_id``, so an operator can pull the entire lifecycle of one delivery
-- ingestion, every retry, the eventual effect -- out of the log stream with a
single filter. The id is honoured from an inbound ``X-Request-Id`` when the
caller supplies one (so it stitches across a proxy), and minted otherwise.

structlog's ``contextvars`` are task-local: two requests handled concurrently on
the same event loop never see each other's id.
"""

from __future__ import annotations

from uuid import uuid4

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_ID_HEADER = "X-Request-Id"
_CORRELATION_ID_KEY = "correlation_id"


class CorrelationIdMiddleware:
    """Bind a correlation id for the duration of each HTTP request.

    Written as raw ASGI rather than ``BaseHTTPMiddleware`` so the context is
    bound in the same task that runs the endpoint; ``BaseHTTPMiddleware`` runs
    the handler in a separate task, and the contextvar would not propagate.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        correlation_id = _inbound_id(scope) or uuid4().hex

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**{_CORRELATION_ID_KEY: correlation_id})

        async def send_with_header(message: Message) -> None:
            # Echo the id back so a client (or a load test) can correlate its
            # request with our logs without guessing.
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((CORRELATION_ID_HEADER.lower().encode(), correlation_id.encode()))
            await send(message)

        try:
            await self._app(scope, receive, send_with_header)
        finally:
            structlog.contextvars.clear_contextvars()


def _inbound_id(scope: Scope) -> str | None:
    """Trust an inbound correlation id only if it is short and printable.

    An unbounded header value copied into every log line is a log-injection
    vector; capping it keeps a hostile caller from writing newlines into our
    structured output.
    """
    target = CORRELATION_ID_HEADER.lower().encode()
    for name, value in scope.get("headers", []):
        if name == target:
            # `scope["headers"]` is typed as bytes pairs but arrives as Any from
            # Starlette; pin the decoded value to str so mypy stays strict here.
            candidate: str = value.decode("latin-1").strip()
            if candidate and len(candidate) <= 128 and candidate.isprintable():
                return candidate
            return None
    return None
