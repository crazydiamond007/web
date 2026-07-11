"""The operator's API: look, triage, replay (FR-15, FR-16, FR-18, FR-20).

Every route here is behind `AdminDep`, so an unauthenticated call is a `401`
before any handler runs (FR-20). Ingestion is *not* behind it -- a provider has no
API key, and authenticates by signature instead.

The read routes never return `payload` or `headers`. This is a support tool, and a
support tool that prints the raw body turns every screenshot pasted into a ticket
into a leak (NFR-6). Everything needed to diagnose an event -- its status, its
attempts, its errors -- is here without it.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Literal, Self

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator

from webhook_receiver.adapters import dlq, query, queue
from webhook_receiver.adapters.database import session_scope
from webhook_receiver.adapters.rng import create_rng
from webhook_receiver.api.auth import AdminDep
from webhook_receiver.api.state import AppStateDep, ClockDep
from webhook_receiver.domain.balance import registry
from webhook_receiver.domain.dlq import InvalidDlqTransitionError
from webhook_receiver.domain.enums import DlqStatus, ReplayOutcome, WebhookStatus
from webhook_receiver.services.replay import EventNotFoundError, replay_events

router = APIRouter(prefix="/v1/admin", tags=["admin"])


# --- Wire models -------------------------------------------------------------


class AttemptOut(BaseModel):
    id: int
    attempt_number: int
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    error_class: str | None
    error_detail: str | None
    duration_ms: int | None


class EventOut(BaseModel):
    id: int
    source: str
    external_id: str
    event_type: str
    entity_type: str
    entity_id: str
    status: str
    attempt_count: int
    occurred_at: datetime
    received_at: datetime
    next_attempt_at: datetime
    processed_at: datetime | None
    last_error: str | None


class EventDetailOut(EventOut):
    attempts: list[AttemptOut]


class DlqOut(BaseModel):
    id: int
    event_id: int
    source: str
    event_type: str
    entity_type: str
    entity_id: str
    external_id: str
    reason: str
    attempts_made: int
    dead_lettered_at: datetime
    status: str
    resolved_at: datetime | None
    resolution_note: str | None


class TriageIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class ReplayIn(BaseModel):
    """What to replay. Exactly one selector (FR-16)."""

    event_ids: list[int] | None = None
    dead_lettered: bool = False
    since: datetime | None = None
    until: datetime | None = None
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> Self:
        selectors = [
            self.event_ids is not None,
            self.dead_lettered,
            self.since is not None or self.until is not None,
        ]
        if sum(selectors) != 1:
            msg = "choose exactly one of: event_ids, dead_lettered, or a since/until range"
            raise ValueError(msg)
        if (self.since is None) != (self.until is None):
            msg = "a time range needs both since and until"
            raise ValueError(msg)
        if self.since is not None and self.until is not None and self.since >= self.until:
            msg = "since must be before until"
            raise ValueError(msg)
        return self


class ReplayResultOut(BaseModel):
    event_id: int
    outcome: ReplayOutcome
    detail: str | None


class ReplayOut(BaseModel):
    requested: int
    results: list[ReplayResultOut]


# --- Routes ------------------------------------------------------------------


@router.get("/events", response_model=list[EventOut], summary="Filter events (FR-18)")
async def list_events(
    state: AppStateDep,
    _: AdminDep,
    status_: Annotated[WebhookStatus | None, Query(alias="status")] = None,
    source: str | None = None,
    event_type: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int | None, Query(gt=0, le=500)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EventOut]:
    async with session_scope(state.session_factory) as session:
        events = await query.list_events(
            session,
            status=status_,
            source=source,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            since=since,
            until=until,
            limit=limit or state.settings.admin_page_size,
            offset=offset,
        )
    return [EventOut(**asdict(event)) for event in events]


@router.get(
    "/events/{event_id}",
    response_model=EventDetailOut,
    summary="One event and its full attempt history (FR-18)",
)
async def get_event(event_id: int, state: AppStateDep, _: AdminDep) -> EventDetailOut:
    async with session_scope(state.session_factory) as session:
        detail = await query.get_event(session, event_id=event_id)

    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such event")

    return EventDetailOut(
        **asdict(detail.event),
        attempts=[AttemptOut(**asdict(attempt)) for attempt in detail.attempts],
    )


@router.get("/dlq", response_model=list[DlqOut], summary="The dead-letter queue (FR-15)")
async def list_dlq(
    state: AppStateDep,
    _: AdminDep,
    status_: Annotated[DlqStatus | None, Query(alias="status")] = None,
    limit: Annotated[int | None, Query(gt=0, le=500)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DlqOut]:
    async with session_scope(state.session_factory) as session:
        entries = await dlq.list_entries(
            session,
            status=status_,
            limit=limit or state.settings.admin_page_size,
            offset=offset,
        )
    return [DlqOut(**asdict(entry)) for entry in entries]


@router.post(
    "/dlq/{entry_id}/{action}",
    response_model=DlqOut,
    summary="Triage a dead-letter entry (FR-15)",
    responses={409: {"description": "Not a legal move from the entry's current status"}},
)
async def triage(
    entry_id: int,
    action: Literal["resolve", "discard"],
    body: TriageIn,
    state: AppStateDep,
    clock: ClockDep,
    _: AdminDep,
) -> DlqOut:
    """Sign an entry off, or decide it does not matter.

    Both are terminal (`domain/dlq.py`): an entry a human has ruled on does not
    quietly reopen. If the same event fails again that is a *new* failure, and it
    deserves a new decision rather than a resurrection that makes the history lie.
    """
    target = DlqStatus.RESOLVED if action == "resolve" else DlqStatus.DISCARDED

    async with session_scope(state.session_factory) as session:
        if await dlq.get_entry(session, entry_id=entry_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no such dead-letter entry"
            )
        try:
            entry = await dlq.transition(
                session, entry_id=entry_id, target=target, now=clock.now(), note=body.note
            )
        except InvalidDlqTransitionError as exc:
            # 409, not 400: the request is well-formed and the operator asked for
            # something coherent. It is the *state* that refuses, and the message
            # says which moves are legal from here.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return DlqOut(**asdict(entry))


@router.post(
    "/replay",
    response_model=ReplayOut,
    summary="Re-process events through the same path (FR-16, FR-17)",
)
async def replay(
    body: ReplayIn,
    state: AppStateDep,
    clock: ClockDep,
    operator: AdminDep,
) -> ReplayOut:
    """Replay a batch of events, the DLQ, or a time range.

    Bounded by `REPLAY_MAX_BATCH`. Replay is synchronous and takes an advisory
    lock per event, so an unbounded "replay everything" would be a self-inflicted
    outage dressed up as a recovery. Page through it instead -- the bound is a
    setting, and the DLQ selector drains oldest-first, so repeating the call makes
    progress.
    """
    settings = state.settings
    limit = settings.replay_max_batch

    async with session_scope(state.session_factory) as session:
        if body.event_ids is not None:
            if len(body.event_ids) > limit:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"asked to replay {len(body.event_ids)} events; the limit is {limit}",
                )
            event_ids = list(body.event_ids)
        elif body.dead_lettered:
            event_ids = list(await queue.dead_lettered_event_ids(session, limit=limit))
        else:
            # The validator guarantees both bounds are present here.
            assert body.since is not None and body.until is not None  # noqa: S101
            event_ids = list(
                await queue.event_ids_in_range(
                    session, since=body.since, until=body.until, limit=limit
                )
            )

    try:
        results = await replay_events(
            state.session_factory,
            event_ids=event_ids,
            requested_by=operator,
            reason=body.reason,
            registry=registry,
            settings=settings,
            clock=clock,
            rng=create_rng(settings.jitter_seed),
        )
    except EventNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return ReplayOut(
        requested=len(event_ids),
        results=[ReplayResultOut(**asdict(result)) for result in results],
    )
