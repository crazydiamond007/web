"""The dead-letter queue as a state machine (FR-15).

A DLQ whose entries can go from any state to any other is not a triage queue, it
is a table of guesses. The value of the DLQ is that an operator can trust what it
says: `resolved` means somebody dealt with it, `discarded` means somebody decided
it did not matter, and neither can be silently undone by a background job that
happens to run afterwards.

    needs_review ──> replaying ──> resolved
         │  │             │
         │  │             └──────> needs_review   (the replay failed; try again)
         │  └────────────────────> resolved       (fixed by hand, outside the app)
         └───────────────────────> discarded

`resolved` and `discarded` are **terminal**. An entry that has been signed off
does not quietly reopen: if the same event fails again it is a new failure and
deserves a new decision, not a resurrection of an old one that makes the history
lie about what happened when.

Pure: no session, no SQL. The adapter asks this module whether a move is legal
and then makes it.
"""

from __future__ import annotations

from typing import Final

from webhook_receiver.domain.enums import DlqStatus
from webhook_receiver.domain.errors import NonRetryableError

_ALLOWED: Final[dict[DlqStatus, frozenset[DlqStatus]]] = {
    DlqStatus.NEEDS_REVIEW: frozenset(
        {DlqStatus.REPLAYING, DlqStatus.RESOLVED, DlqStatus.DISCARDED}
    ),
    # A replay that failed puts the entry back in the queue for a human, rather
    # than leaving it stuck in `replaying` forever with nothing watching it.
    DlqStatus.REPLAYING: frozenset(
        {DlqStatus.RESOLVED, DlqStatus.NEEDS_REVIEW, DlqStatus.DISCARDED}
    ),
    DlqStatus.RESOLVED: frozenset(),
    DlqStatus.DISCARDED: frozenset(),
}

TERMINAL: Final[frozenset[DlqStatus]] = frozenset({DlqStatus.RESOLVED, DlqStatus.DISCARDED})


class InvalidDlqTransitionError(NonRetryableError):
    """The requested move is not one this state machine allows.

    Non-retryable by inheritance, and that is right: asking again will be refused
    again. It surfaces as a `409` on the admin API, not a `500` -- the operator
    asked for something coherent, it is simply not legal from where the entry is.
    """


def ensure_transition(current: DlqStatus, target: DlqStatus) -> None:
    """Raise unless `current -> target` is a legal move."""
    if target not in _ALLOWED[current]:
        allowed = ", ".join(sorted(s.value for s in _ALLOWED[current])) or "nothing"
        was_terminal = " (it is terminal)" if current in TERMINAL else ""
        msg = (
            f"cannot move a dead-letter entry from {current.value!r} to {target.value!r}"
            f"{was_terminal}; from {current.value!r} the legal moves are: {allowed}"
        )
        raise InvalidDlqTransitionError(msg)
