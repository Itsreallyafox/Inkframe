"""System routes: health, the status snapshot the Now page polls, log tail."""

from __future__ import annotations

import logging
import shutil
import socket
import sqlite3
import time
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from inkframe import __version__
from inkframe.api import DbDep, DisplayDep, PathsDep, SettingsDep
from inkframe.db import Database
from inkframe.log import RING_BUFFER_LINES, get_recent_logs
from inkframe.scheduler import iso, local_now, quiet_window_active

log = logging.getLogger("inkframe.api.system")

router = APIRouter(prefix="/api", tags=["system"])

PANEL_STATES = ("asleep", "awake", "refreshing")


def _db_state(db: Database, key: str) -> Any:
    """``db.get_state(key)``, or None when the row cannot be read (text that is
    not JSON, a failing database): the status page reports ``null`` instead
    of failing with a 500."""
    try:
        return db.get_state(key)
    except (sqlite3.Error, ValueError) as e:
        log.debug("status: state %r is unreadable (%s: %s)", key, type(e).__name__, e)
        return None


# HEAD as its own route, kept out of the schema: one route with both methods
# gives two OpenAPI operations with the same id (a warning per /openapi.json)
@router.get("/health")
@router.head("/health", include_in_schema=False)
def health() -> dict[str, bool]:
    """Liveness probe for deploy scripts."""
    return {"ok": True}


@router.get("/status")
def status(
    request: Request, store: SettingsDep, db: DbDep, display: DisplayDep, paths: PathsDep
) -> dict[str, Any]:
    """Everything the Now page shows.  The shape is a contract with the UI:
    every key is always present; unknown values are ``null``.

    ``panel.last_refresh_at``/``last_source`` come from the db (the scheduler
    mirrors them there on every refresh; an unreadable row falls back to the
    scheduler's own value); everything else under ``panel`` and ``rotation``
    comes from the scheduler's snapshot when there is one.
    """
    settings = store.get()
    state = request.app.state
    last_refresh_at = _db_state(db, "last_refresh_at")
    last_source = _db_state(db, "last_source")
    # both drivers expose ``state`` (asleep/awake/refreshing); the protocol only has ``awake``
    panel_state = getattr(display, "state", None)
    if panel_state not in PANEL_STATES:
        panel_state = "awake" if display.awake else "asleep"
    quiet, quiet_until = quiet_window_active(settings.quiet_hours, local_now())
    panel: dict[str, Any] = {
        "state": panel_state,
        "last_refresh_at": last_refresh_at if isinstance(last_refresh_at, str) else None,
        "last_attempt_at": None,
        "last_source": last_source if isinstance(last_source, dict) else None,
        "last_error": None,
    }
    rotation: dict[str, Any] = {
        "paused": settings.paused,
        "quiet": quiet,
        "quiet_until": iso(quiet_until),
        "current": None,
        "next": None,
        "next_at": None,
        "keepalive_at": None,
        "pending": None,
    }
    scheduler = getattr(state, "scheduler", None)
    if scheduler is not None:
        snap = scheduler.snapshot()
        for key in ("last_refresh_at", "last_source"):
            if panel[key] is None:
                panel[key] = snap["panel"][key]
        panel["last_attempt_at"] = snap["panel"]["last_attempt_at"]
        panel["last_error"] = snap["panel"]["last_error"]
        rotation.update(snap["rotation"])
    return {
        "version": __version__,
        "display": {
            "kind": display.kind,
            "width": display.width,
            "height": display.height,
            "awake": display.awake,
        },
        "panel": panel,
        "rotation": rotation,
        "settings": {
            "refresh_floor_seconds": settings.panel.refresh_floor_seconds,
            "keepalive_hours": settings.panel.keepalive_hours,
            "quiet_hours": settings.quiet_hours.to_dict(),
            "clear_before_refresh": settings.display.clear_before_refresh,
        },
        "system": {
            "uptime_seconds": round(time.monotonic() - state.started_monotonic, 3),
            "hostname": socket.gethostname(),
            "data_dir": str(paths.root),
            "disk_free_bytes": shutil.disk_usage(paths.root).free,
            "display_env": state.display_env,
        },
    }


@router.get("/system/logs")
def logs(
    n: Annotated[int, Query(ge=1, le=RING_BUFFER_LINES)] = 200,
) -> dict[str, list[str]]:
    """The last ``n`` log lines kept in memory (oldest first)."""
    return {"lines": get_recent_logs(n)}
