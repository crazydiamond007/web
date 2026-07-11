"""`GET /metrics` (FR-19).

Unauthenticated, deliberately. A Prometheus scraper does not carry an admin key,
and the endpoint exposes counts and latencies -- never a payload, an entity id, or
a secret (see `obs/metrics.py`: every label is drawn from a closed set). In a real
deployment it is bound to the internal network and not routed publicly; that is a
deployment decision, not an application one, and putting an API key on it here
would only mean the scraper's config file holds the admin key.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from webhook_receiver.obs import metrics

router = APIRouter(tags=["observability"])


@router.get("/metrics", summary="Prometheus exposition", include_in_schema=False)
async def scrape() -> Response:
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)
