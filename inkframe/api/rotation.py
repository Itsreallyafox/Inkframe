"""``GET/PUT /api/rotation``: the photo stream and the cards the frame cycles through.

The model lives in ``settings.rotation`` (one source of truth with
``/api/settings``); this route adds what the Rotation page needs around it:
every registered app is listed (a stored turn with its settings, any other
app switched off with a default), with its name and description, and app
ids are checked against the registry, which the settings tree cannot do on
its own.  An id that is stored already passes even when it is no longer
registered (an app a later release removed), so the page can keep saving
until that card is dropped.  Validation messages from the tree
(``rotation.apps[1].minutes must be between 1 and 1440, got 0``) pass
through as the 422 ``error``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request

from inkframe.api import AppsDep, SettingsDep
from inkframe.apps.base import App
from inkframe.config import Settings
from inkframe.scheduler import PHOTOS_APP_ID

router = APIRouter(prefix="/api", tags=["rotation"])

#: minutes a registered app that has no stored turn is listed with (switched off)
DEFAULT_APP_MINUTES = 2
_BODY_KEYS = ("photos", "apps", "paused")
#: read-only keys the GET shape adds to each app entry; accepted (and ignored) on PUT
_DERIVED_KEYS = ("name", "description")


def rotation_view(settings: Settings, apps: Mapping[str, App]) -> dict[str, Any]:
    """The ``GET /api/rotation`` shape.

    ``apps`` lists the stored turns in order, then every other registered
    app (except ``photos``) switched off with ``DEFAULT_APP_MINUTES``, so the
    page always shows every app and one added by an upgrade appears off.  A
    stored id that is no longer registered keeps its row, named by its id."""
    turns = []
    listed: set[str] = set()
    for turn in settings.rotation.apps:
        app = apps.get(turn.app_id)
        turns.append(
            {
                **turn.to_dict(),
                "name": app.name if app is not None else turn.app_id,
                "description": app.description if app is not None else "",
            }
        )
        listed.add(turn.app_id)
    for app_id, app in apps.items():
        if app_id == PHOTOS_APP_ID or app_id in listed:
            continue
        turns.append(
            {
                "app_id": app_id,
                "enabled": False,
                "minutes": DEFAULT_APP_MINUTES,
                "name": app.name,
                "description": app.description,
            }
        )
    return {
        "photos": settings.rotation.photos.to_dict(),
        "apps": turns,
        "paused": settings.paused,
        "quiet_hours": settings.quiet_hours.to_dict(),
    }


@router.get("/rotation")
def read_rotation(store: SettingsDep, apps: AppsDep) -> dict[str, Any]:
    """The photo stream, every app with its turn, the paused flag, quiet hours."""
    return rotation_view(store.get(), apps)


def _clean_apps(
    raw: object, apps: Mapping[str, App], stored: Collection[str]
) -> list[dict[str, Any]]:
    """The ``apps`` list of a PUT body without the derived keys, every id a
    registered app or one in ``stored`` (the ids ``settings.rotation.apps``
    holds now: a card whose app is gone stays saveable, as the GET shape
    lists it).  Everything else is left to the settings tree, whose messages
    name the field."""
    if not isinstance(raw, list):
        raise ValueError(f"apps must be a list, got {type(raw).__name__}")
    known = [a for a in apps if a != PHOTOS_APP_ID]
    out = []
    for i, item in enumerate(raw):
        where = f"apps[{i}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{where} must be an object")
        turn = {k: v for k, v in item.items() if k not in _DERIVED_KEYS}
        app_id = turn.get("app_id")
        # photos passes here so the tree can say where it belongs
        if not isinstance(app_id, str) or (app_id not in apps and app_id not in stored):
            raise ValueError(f"{where}.app_id: unknown app {app_id!r}; known: {', '.join(known)}")
        out.append(turn)
    return out


@router.put("/rotation")
def update_rotation(
    body: Annotated[dict[str, Any], Body()], request: Request, store: SettingsDep, apps: AppsDep
) -> dict[str, Any]:
    """``{"photos": {...}?, "apps": [...]?, "paused"?: bool}``, at least one
    of ``photos``/``apps``.  ``photos`` patches the stream key by key;
    ``apps`` replaces the whole list.  Invalid input: 422 with the reason;
    nothing changes.  Wakes the scheduler and returns the GET shape."""
    unknown = sorted(k for k in body if k not in _BODY_KEYS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown key(s): {', '.join(unknown)}")
    if "photos" not in body and "apps" not in body:
        raise HTTPException(status_code=422, detail="photos or apps is required")
    try:
        rotation: dict[str, Any] = {}
        if "photos" in body:
            rotation["photos"] = body["photos"]
        if "apps" in body:
            stored = [t.app_id for t in store.get().rotation.apps]
            rotation["apps"] = _clean_apps(body["apps"], apps, stored)
        patch: dict[str, Any] = {"rotation": rotation}
        if "paused" in body:
            patch["paused"] = body["paused"]
        settings = store.patch(patch)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.wake()
    return rotation_view(settings, apps)
