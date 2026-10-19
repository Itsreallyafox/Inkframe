"""System routes: health, the status snapshot the Now page polls, log tail."""

from __future__ import annotations

import shutil
import socket
import time
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from inkframe import __version__
from inkframe.api import DbDep, DisplayDep, PathsDep, SettingsDep
from inkframe.log import RING_BUFFER_LINES, get_recent_logs

router = APIRouter(prefix="/api", tags=["system"])

PANEL_STATES = ("asleep", "awake", "refreshing")


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
    every key is always present; unknown values are ``null``."""
    settings = store.get()
    state = request.app.state
    last_source = db.get_state("last_source")
    # both drivers expose ``state`` (asleep/awake/refreshing); the protocol only has ``awake``
    panel_state = getattr(display, "state", None)
    if panel_state not in PANEL_STATES:
        panel_state = "awake" if display.awake else "asleep"
    return {
        "version": __version__,
        "display": {
            "kind": display.kind,
            "width": display.width,
            "height": display.height,
            "awake": display.awake,
        },
        "panel": {
            "state": panel_state,
            "last_refresh_at": db.get_state("last_refresh_at"),
            "last_source": last_source if isinstance(last_source, dict) else None,
        },
        "rotation": {
            "paused": settings.paused,
            "current": None,
            "next": None,
            "next_at": None,
            "keepalive_at": None,
        },
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
