"""Dispatch an event to the code that knows what it means (FR-8).

A registry rather than an ``if/elif`` chain, for one reason that matters: adding
a provider event type should not require editing the worker. The worker knows how
to claim, lock, apply, and record; it does not know what `balance.credited` is,
and it should not have to.

An unregistered event type raises ``UnknownEventTypeError`` -- it is never
skipped. See that class for why silence would be the worst possible behaviour.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from webhook_receiver.domain.effects import Effect
from webhook_receiver.domain.errors import UnknownEventTypeError
from webhook_receiver.domain.events import StoredEvent


class Handler(Protocol):
    """Interpret one event type. Pure: no I/O, no session, no clock.

    Raise ``NonRetryableError`` if the event cannot be acted on, and
    ``RetryableError`` if the world was temporarily uncooperative. Returning an
    ``Effect`` means "this is what should happen"; whether it *has* already
    happened is the adapter's problem, not the handler's -- which is precisely
    why a handler can be called twice with no harm.
    """

    def __call__(self, event: StoredEvent) -> Effect: ...


class HandlerRegistry:
    """Maps `event_type` to the handler that understands it."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, event_type: str) -> Callable[[Handler], Handler]:
        """Decorator form: ``@registry.register("balance.credited")``."""

        def decorate(handler: Handler) -> Handler:
            if event_type in self._handlers:
                # A silent overwrite means two handlers disagree about an event
                # type and the winner depends on import order. Fail at import.
                msg = f"a handler is already registered for event type {event_type!r}"
                raise ValueError(msg)
            self._handlers[event_type] = handler
            return handler

        return decorate

    def dispatch(self, event: StoredEvent) -> Effect:
        handler = self._handlers.get(event.event_type)
        if handler is None:
            msg = f"no handler registered for event type {event.event_type!r}"
            raise UnknownEventTypeError(msg)
        return handler(event)

    @property
    def event_types(self) -> frozenset[str]:
        return frozenset(self._handlers)
