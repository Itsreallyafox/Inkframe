"""``GET/PUT /api/settings``: the whole settings tree as JSON."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request

from inkframe.api import SettingsDep

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings")
def read_settings(store: SettingsDep) -> dict[str, Any]:
    """The full settings object (the shape of ``settings.json``)."""
    return store.get().to_dict()


@router.put("/settings")
def update_settings(
    patch: Annotated[dict[str, Any], Body()], request: Request, store: SettingsDep
) -> dict[str, Any]:
    """Deep-merge a full or partial settings object over the current settings
    and return the saved result.

    Nested objects merge key by key; lists (``rotation``, ``quiet_hours.days``)
    are replaced whole.  Unknown keys or invalid values: 422 with the reason.
    The scheduler is woken so a new floor, quiet window or rotation applies now;
    a palette or default-recipe change also marks the panel stale so the photo on
    it is repainted at the next rotation change.
    """
    try:
        saved = store.patch(patch).to_dict()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        if "palette" in patch or "default_recipe" in patch:
            # the photo on the panel may now render differently: repaint it at
            # the next rotation change instead of skipping it as already showing
            scheduler.invalidate()
        scheduler.wake()
    return saved
