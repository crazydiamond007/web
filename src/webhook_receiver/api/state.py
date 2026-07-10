"""Typed application state, injected rather than reached for.

FastAPI's ``app.state`` is an untyped bag. Wrapping it in a frozen dataclass and
handing it out through a dependency keeps `mypy --strict` meaningful and lets
tests swap the engine without monkeypatching a module global.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from webhook_receiver.adapters.clock import Clock, SystemClock
from webhook_receiver.config import Settings

STATE_ATTR = "webhook_receiver_state"


@dataclass(frozen=True, slots=True)
class AppState:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


def get_app_state(request: Request) -> AppState:
    # cast: Starlette types `app.state` attribute access as Any. The value is
    # only ever set by `create_app`, which sets it to an AppState.
    return cast(AppState, getattr(request.app.state, STATE_ATTR))


AppStateDep = Annotated[AppState, Depends(get_app_state)]


def get_clock() -> Clock:
    """The application clock (SPEC §6.4).

    A dependency, not a bare `SystemClock()`, so a test can override it through
    FastAPI and pin `now` -- verifying the endpoint's stale-timestamp path
    without waiting out the real tolerance window.
    """
    return SystemClock()


ClockDep = Annotated[Clock, Depends(get_clock)]
