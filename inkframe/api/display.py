"""``/api/display/*``: the Now page's controls (next, pause, resume, show now).

Nothing here touches the panel directly: every route hands a request to the
scheduler, which owns the panel and its care rules, and answers with the
scheduler's snapshot (the ``panel`` and ``rotation`` parts of ``/api/status``)
so the UI can update its countdown without a second request.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from inkframe.api import SchedulerDep
from inkframe.scheduler import Scheduler

router = APIRouter(prefix="/api/display", tags=["display"])

_SHOW_KEYS = ("app_id", "photo_id")


def _result(scheduler: Scheduler) -> dict[str, Any]:
    return {"ok": True, **scheduler.snapshot()}


@router.post("/next")
def show_next(scheduler: SchedulerDep) -> dict[str, Any]:
    """Show the next rotation item as soon as the panel allows (show-now rules:
    the refresh floor is bypassed, the show-now minimum is not)."""
    scheduler.show_next()
    return _result(scheduler)


@router.post("/pause")
def pause(scheduler: SchedulerDep) -> dict[str, Any]:
    """Pause the rotation (persisted).  Keep-alive and show-now still work."""
    scheduler.pause()
    return _result(scheduler)


@router.post("/resume")
def resume(scheduler: SchedulerDep) -> dict[str, Any]:
    """Resume the rotation."""
    scheduler.resume()
    return _result(scheduler)


@router.post("/show")
def show(body: Annotated[dict[str, Any], Body()], scheduler: SchedulerDep) -> dict[str, Any]:
    """Queue ``{"app_id": ...}`` or ``{"photo_id": ...}`` for the panel.

    A newer request replaces a pending one; ``rotation.pending.fires_at`` in
    the answer is when it will be shown.  Unknown id: 404.  Both keys, neither,
    a null value, or anything else in the body: 422.
    """
    unknown = sorted(k for k in body if k not in _SHOW_KEYS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown key(s): {', '.join(unknown)}")
    present = [k for k in _SHOW_KEYS if k in body]
    if len(present) != 1 or body[present[0]] is None:
        raise HTTPException(
            status_code=422, detail="pass exactly one of app_id or photo_id (not null)"
        )
    key = present[0]
    value = body[key]
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{key} must be a non-empty string")
    try:
        if key == "app_id":
            scheduler.show_now(app_id=value.strip())
        else:
            scheduler.show_now(photo_id=value.strip())
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]) if e.args else str(e)) from None
    return _result(scheduler)
