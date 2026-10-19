"""``GET/PUT /api/rotation``: the ordered app slots the frame cycles through.

The slots live in ``settings.rotation`` (one source of truth with
``/api/settings``); this route adds what the Rotation page needs around them
(app names and descriptions, the known app ids) and validates more strictly
than the settings tree can on its own: app ids must be known apps and the
photos slot's options are checked (``dwell_minutes`` 1..1440, ``order``
``shuffle``/``sequential``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request

from inkframe.api import AppsDep, SettingsDep
from inkframe.apps.base import App
from inkframe.config import Settings
from inkframe.scheduler import PHOTOS_APP_ID, photo_options, validate_photo_options

router = APIRouter(prefix="/api", tags=["rotation"])

_BODY_KEYS = ("slots", "paused")
#: read-only keys the GET shape adds to each slot; accepted (and ignored) on PUT
_DERIVED_SLOT_KEYS = ("name", "description")


def rotation_view(settings: Settings, apps: Mapping[str, App]) -> dict[str, Any]:
    """The ``GET /api/rotation`` shape.

    The photos slot's options are reported as the scheduler reads them
    (``dwell_minutes`` and ``order``, defaults filled in) and nothing else:
    keys that ``/api/settings`` stored but the scheduler ignores are left
    out, so a GET body can always be PUT back."""
    slots = []
    for slot in settings.rotation:
        d = slot.to_dict()
        if slot.app_id == PHOTOS_APP_ID:
            dwell, order = photo_options(slot.options)
            minutes = dwell.total_seconds() / 60
            d["options"] = {
                "dwell_minutes": int(minutes) if minutes.is_integer() else minutes,
                "order": order,
            }
        app = apps.get(slot.app_id)
        d["name"] = app.name if app is not None else slot.app_id
        d["description"] = app.description if app is not None else ""
        slots.append(d)
    return {
        "slots": slots,
        "paused": settings.paused,
        "quiet_hours": settings.quiet_hours.to_dict(),
        "apps": list(apps),
    }


@router.get("/rotation")
def read_rotation(store: SettingsDep, apps: AppsDep) -> dict[str, Any]:
    """Slots in order with app names, the paused flag, quiet hours, app ids."""
    return rotation_view(store.get(), apps)


def _clean_slots(raw: object, apps: Mapping[str, App]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError(f"slots must be a list, got {type(raw).__name__}")
    out = []
    for i, item in enumerate(raw):
        where = f"slots[{i}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{where} must be an object")
        slot = {k: v for k, v in item.items() if k not in _DERIVED_SLOT_KEYS}
        app_id = slot.get("app_id")
        if not isinstance(app_id, str) or app_id not in apps:
            raise ValueError(f"{where}.app_id: unknown app {app_id!r}; known: {', '.join(apps)}")
        if app_id == PHOTOS_APP_ID:
            try:
                slot["options"] = validate_photo_options(slot.get("options"))
            except ValueError as e:
                raise ValueError(f"{where}.{e}") from None
        out.append(slot)
    return out


@router.put("/rotation")
def update_rotation(
    body: Annotated[dict[str, Any], Body()], request: Request, store: SettingsDep, apps: AppsDep
) -> dict[str, Any]:
    """Replace the whole slot list (``{"slots": [...]}``, optionally
    ``"paused"``).  Invalid input: 422 with the reason; nothing changes."""
    unknown = sorted(k for k in body if k not in _BODY_KEYS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown key(s): {', '.join(unknown)}")
    if "slots" not in body:
        raise HTTPException(status_code=422, detail="slots is required")
    try:
        patch: dict[str, Any] = {"rotation": _clean_slots(body["slots"], apps)}
        if "paused" in body:
            patch["paused"] = body["paused"]
        settings = store.patch(patch)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.wake()
    return rotation_view(settings, apps)
