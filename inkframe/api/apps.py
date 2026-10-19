"""``/api/apps``: the app list, per-app settings and browser previews.

Previews render on the app's one-thread render executor (never the event
loop, never the panel) with the same ``RenderContext`` the scheduler builds,
and come back as a PNG in the panel's *measured* colours, i.e. what the
frame would look like.  ``INKFRAME_OFFLINE=1`` makes previews offline too.
"""

from __future__ import annotations

import asyncio
import functools
import io
import logging
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import Response

from inkframe.api import AppsDep, DbDep
from inkframe.apps.base import App, FontLibrary, render_to_frame, schema_to_json, validate_settings
from inkframe.config import SettingsStore
from inkframe.db import Database
from inkframe.display.palette import indices_to_image
from inkframe.paths import DataPaths
from inkframe.scheduler import (
    PHOTOS_APP_ID,
    Scheduler,
    app_settings_for,
    build_render_context,
    local_now,
)

log = logging.getLogger("inkframe.api.apps")

router = APIRouter(prefix="/api/apps", tags=["apps"])


def _app(apps: Mapping[str, App], app_id: str) -> App:
    app = apps.get(app_id)
    if app is None:
        raise HTTPException(status_code=404, detail=f"unknown app {app_id!r}")
    return app


def _app_info(app: App, db: Database) -> dict[str, Any]:
    return {
        "id": app.id,
        "name": app.name,
        "description": app.description,
        "needs_dither": bool(app.needs_dither),
        "variants": int(app.variants),
        "settings_schema": schema_to_json(app.settings_schema),
        "settings": app_settings_for(app, db),
    }


@router.get("")
def list_apps(apps: AppsDep, db: DbDep) -> list[dict[str, Any]]:
    """Every app with its settings schema (for the generated forms) and its
    current, validated settings."""
    return [_app_info(app, db) for app in apps.values()]


@router.get("/{app_id}/settings")
def read_app_settings(app_id: str, apps: AppsDep, db: DbDep) -> dict[str, Any]:
    """The app's complete settings (stored values over the schema defaults)."""
    return app_settings_for(_app(apps, app_id), db)


@router.put("/{app_id}/settings")
def update_app_settings(
    app_id: str, body: Annotated[dict[str, Any], Body()], apps: AppsDep, db: DbDep
) -> dict[str, Any]:
    """Merge a partial settings object over the stored one, validate it against
    the app's schema and save it.  Returns the complete settings; 422 with the
    reason if anything does not validate (nothing is saved then).

    Only keys the user has set are stored, so a required field left unset
    (e.g. the weather location) keeps prompting instead of being saved empty.
    """
    app = _app(apps, app_id)
    keys = {f.key for f in app.settings_schema}
    # stored keys an older schema had are dropped; unknown keys in the body are a 422
    stored = {k: v for k, v in db.get_app_settings(app_id).items() if k in keys}
    merged = {**stored, **body}
    try:
        validated = validate_settings(app.settings_schema, merged)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    db.set_app_settings(app_id, {k: validated[k] for k in merged})
    return validated


def render_preview_png(
    app: App,
    *,
    store: SettingsStore,
    db: Database,
    paths: DataPaths,
    fonts: FontLibrary | None,
    scheduler: Scheduler | None,
    variant: int | None,
    seed: int | None,
) -> bytes:
    """Render ``app`` like the scheduler would and encode it as a PNG in the
    measured palette.  Runs on the render executor."""
    settings = store.get()
    item_id = None
    if app.id == PHOTOS_APP_ID:
        # the photo on the panel if there is one, else the first included photo
        current = scheduler.current_photo_id() if scheduler is not None else None
        if current is not None and db.get_photo(current) is not None:
            item_id = current
        else:
            photos = db.list_photos(include_excluded=False)
            item_id = photos[0].id if photos else None
    ctx = build_render_context(
        app,
        settings=settings,
        db=db,
        paths=paths,
        now=local_now(),
        fonts=fonts,
        online=True,
        item_id=item_id,
        variant=variant,
        seed=seed,
    )
    frame = render_to_frame(
        app, ctx, recipe=settings.default_recipe, render_palette=settings.render_palette()
    )
    buf = io.BytesIO()
    indices_to_image(frame, settings.measured_palette()).save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


@router.get("/{app_id}/preview.png")
async def preview(
    app_id: str,
    request: Request,
    variant: Annotated[int | None, Query(ge=0, le=99)] = None,
    seed: Annotated[int | None, Query(ge=0, le=2**63 - 1)] = None,
) -> Response:
    """A preview render of the app (``variant`` forces a layout, ``seed`` a
    generative draw).  Never touches the panel.  App failure: 500."""
    state = request.app.state
    app = _app(state.apps, app_id)
    if variant is not None and variant >= app.variants:
        raise HTTPException(
            status_code=422, detail=f"variant must be 0..{app.variants - 1} for {app_id}"
        )
    job = functools.partial(
        render_preview_png,
        app,
        store=state.settings,
        db=state.db,
        paths=state.paths,
        fonts=getattr(state, "fonts", None),
        scheduler=getattr(state, "scheduler", None),
        variant=variant,
        seed=seed,
    )
    try:
        png = await asyncio.get_running_loop().run_in_executor(state.render_executor, job)
    except Exception as e:  # noqa: BLE001 - any app failure becomes a 500 with its message
        log.exception("preview of %s failed", app_id)
        message = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        raise HTTPException(status_code=500, detail=f"{app_id} preview failed: {message}") from None
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})
