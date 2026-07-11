"""What went wrong, and whether trying again could possibly help (FR-11).

The taxonomy exists because "retry on failure" is not a policy -- it is a way to
turn one bug into a thousand. A malformed payload will be just as malformed on
the fifth attempt; retrying it burns the retry budget, delays the events behind
it, and buries the real signal. So every failure has to answer one question
before anything else: *is this a fact about the world, or a fact about the
event?*

* ``RetryableError`` -- a fact about the world. A timeout, a lock we could not
  get, a downstream 503. The same event, sent again later, may well succeed.
* ``NonRetryableError`` -- a fact about the event. No amount of waiting changes
  it, so it goes straight to the dead-letter queue for a human to look at.

Ruff's ``BLE`` rule forbids a bare ``except`` anywhere in this codebase (SPEC
§6.6), which forces the question to be answered at every catch site rather than
swallowed. An exception that is *neither* of these -- a genuine bug in our code
-- must be allowed to propagate; see ``services/process.py`` for how an unknown
exception is treated (conservatively, as retryable, but recorded by class name).
"""

from __future__ import annotations


class ProcessingError(Exception):
    """Base for every failure a handler is allowed to signal."""


class RetryableError(ProcessingError):
    """Transient. The same event may succeed on a later attempt (FR-11, FR-12)."""


class NonRetryableError(ProcessingError):
    """Permanent. Retrying cannot change the outcome, so do not (FR-11, FR-14)."""


class LockContentionError(RetryableError):
    """Another worker holds this entity's lock and would not let go in time (FR-9).

    Not a failure of the event: the event is fine, the entity was simply busy.
    Retrying is the *entire* point -- by the time we come back, the worker ahead
    of us has committed, and we will see the state it wrote.
    """


class UnknownEventTypeError(NonRetryableError):
    """No handler is registered for this ``event_type`` (FR-8).

    Deliberately an error rather than a silent skip. An event type we do not
    recognise is either a provider adding something new or us forgetting to
    register a handler -- both are things a human needs to see. Dropping it
    quietly would make the event vanish while still reporting `succeeded`.
    """


class UnprocessableEventError(NonRetryableError):
    """The event authenticated and routed, but the handler cannot act on it.

    A missing field, a field of the wrong type, an entity this handler does not
    own, or a state-setting event with no ordering key. Distinct from the wire
    layer's ``MalformedPayloadError``, which rejects a body *before* it is ever
    stored: by the time we are here the event is durably persisted, so the
    failure has to be recorded against the row rather than answered with a 400.
    """
