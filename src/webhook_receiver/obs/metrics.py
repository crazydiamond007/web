"""Prometheus metrics (FR-19, NFR-5).

The metrics are chosen to answer the questions someone actually asks at 3am, and
no others:

* *Are we losing events?*      `webhook_events_ingested_total` vs `..._processed_total`
* *Is something poisoned?*     `webhook_events_dead_lettered_total`, by reason
* *Is a downstream sick?*      `webhook_events_retried_total`, by error class
* *Are we slow, and where?*    the two histograms -- ingestion and processing are
  separate because they fail differently: ingestion latency is the provider's
  problem (they time out and redeliver), processing latency is ours.

**Labels are bounded.** Every label value here is drawn from a closed set -- an
enum, a configured source name, an exception class name. None of them is an event
id, an entity id, or anything else an attacker or a busy provider can invent. An
unbounded label is not a metric, it is a memory leak with a dashboard: Prometheus
keeps a distinct time series per label combination, forever, and a `entity_id`
label on a million accounts is a million series in the scrape.

`source` and `event_type` are the borderline cases. `source` is safe -- it comes
from `WEBHOOK_SECRETS`, so an unconfigured one is rejected at the door with a 401
and never reaches a counter. `event_type` is *not* bounded by configuration, so it
is only ever recorded for types we have a handler for; an unknown type increments
a single `unknown` bucket rather than minting a series per garbage string.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# Ingestion is measured in milliseconds and processing in seconds, so they get
# different buckets. Sharing one set would put every ingestion in the first bucket
# and every processing in the last, which is a histogram that has learned nothing.
INGEST_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)
PROCESS_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

UNKNOWN_EVENT_TYPE = "unknown"

events_ingested = Counter(
    "webhook_events_ingested_total",
    "Deliveries accepted at the wire, whether new or duplicate.",
    labelnames=("source", "duplicate"),
)

events_rejected = Counter(
    "webhook_events_rejected_total",
    "Deliveries refused before storage: bad signature, unroutable body, oversized.",
    labelnames=("source", "reason"),
)

events_processed = Counter(
    "webhook_events_processed_total",
    "Events taken to a terminal state by a worker.",
    labelnames=("event_type", "outcome"),
)

events_retried = Counter(
    "webhook_events_retried_total",
    "Retryable failures rescheduled with backoff.",
    labelnames=("event_type", "error_class"),
)

events_dead_lettered = Counter(
    "webhook_events_dead_lettered_total",
    "Events that will not be retried again without a human.",
    labelnames=("event_type", "reason"),
)

events_replayed = Counter(
    "webhook_events_replayed_total",
    "Events re-processed on an operator's request.",
    labelnames=("outcome",),
)

ingest_latency = Histogram(
    "webhook_ingest_duration_seconds",
    "Wall time from request received to 200 returned (NFR-2).",
    labelnames=("source",),
    buckets=INGEST_BUCKETS,
)

process_latency = Histogram(
    "webhook_process_duration_seconds",
    "Wall time for one processing attempt, lock wait included.",
    labelnames=("event_type",),
    buckets=PROCESS_BUCKETS,
)


def known_event_type(event_type: str, known: frozenset[str]) -> str:
    """Collapse an unregistered event type to a single bucket.

    `event_type` comes off the wire, so a provider (or an attacker with a valid
    signature) could otherwise mint a new time series per request and blow up the
    scrape. Only types we have a handler for get their own label.
    """
    return event_type if event_type in known else UNKNOWN_EVENT_TYPE


def render() -> tuple[bytes, str]:
    """The exposition payload and its content type."""
    return generate_latest(), CONTENT_TYPE_LATEST
