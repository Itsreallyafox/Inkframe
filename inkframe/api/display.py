"""``/api/display/*``: the Now page's controls (next, pause, resume, show now)
and the frame on the panel (``frame.png``).

Nothing here touches the panel directly: every route hands a request to the
scheduler, which owns the panel and its care rules, and answers with the
scheduler's snapshot (the ``panel`` and ``rotation`` parts of ``/api/status``)
so the UI can update its countdown without a second request.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request, Response

from inkframe.api import PathsDep, SchedulerDep
from inkframe.scheduler import Scheduler

router = APIRouter(prefix="/api/display", tags=["display"])

_SHOW_KEYS = ("app_id", "photo_id")
#: optional with ``app_id`` only: force one of the app's layout variants
_VARIANT_KEY = "variant"


def _result(scheduler: Scheduler) -> dict[str, Any]:
    return {"ok": True, **scheduler.snapshot()}


@router.get("/frame.png")
def frame_png(request: Request, paths: PathsDep) -> Response:
    """The frame on the panel (``<data>/last_frame.png``, in the measured
    colours), with ``no-cache`` and an ETag from its mtime and size so the
    Now page's revalidation is a cheap 304.  404 until the first refresh."""
    path = paths.last_frame_png
    try:
        st = path.stat()
    except OSError:
        raise HTTPException(status_code=404, detail="nothing has been shown yet") from None
    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    headers = {"Cache-Control": "no-cache", "ETag": etag}
    match = request.headers.get("if-none-match", "")
    if etag in (tag.strip().removeprefix("W/") for tag in match.split(",")):
        return Response(status_code=304, headers=headers)
    try:
        data = path.read_bytes()  # written atomically by the scheduler
    except OSError:
        raise HTTPException(status_code=404, detail="nothing has been shown yet") from None
    return Response(content=data, media_type="image/png", headers=headers)


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

    ``{"app_id": ..., "variant": n}`` forces one of the app's layout variants
    for this one showing (the Settings page shows the patterns app's palette
    test card, variant 2, without changing the pattern settings).

    A newer request replaces a pending one; ``rotation.pending.fires_at`` in
    the answer is when it will be shown.  Unknown id: 404.  Both keys, neither,
    a null value, a variant without app_id or out of range, or anything else
    in the body: 422.
    """
    unknown = sorted(k for k in body if k not in (*_SHOW_KEYS, _VARIANT_KEY))
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
    variant = body.get(_VARIANT_KEY)
    if _VARIANT_KEY in body:
        if key != "app_id":
            raise HTTPException(status_code=422, detail="variant goes with app_id only")
        if isinstance(variant, bool) or not isinstance(variant, int) or variant < 0:
            raise HTTPException(
                status_code=422, detail=f"variant must be a whole number >= 0, got {variant!r}"
            )
    try:
        if key == "app_id":
            scheduler.show_now(app_id=value.strip(), variant=variant)
        else:
            scheduler.show_now(photo_id=value.strip())
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]) if e.args else str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return _result(scheduler)
