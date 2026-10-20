"""System routes: health, the status snapshot the Now page polls, log tail,
restart."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import sqlite3
import threading
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from inkframe import __version__
from inkframe.api import DbDep, DisplayDep, PathsDep, SettingsDep
from inkframe.db import Database
from inkframe.log import RING_BUFFER_LINES, get_recent_logs
from inkframe.paths import DataPaths
from inkframe.scheduler import iso, local_now, quiet_window_active

log = logging.getLogger("inkframe.api.system")

router = APIRouter(prefix="/api", tags=["system"])

PANEL_STATES = ("asleep", "awake", "refreshing")

#: how long ``POST /api/system/restart`` waits before signalling itself, so
#: the ``{"ok": true}`` answer reaches the browser first
RESTART_DELAY_SECONDS = 0.5
#: the storage walk is reused this long: the Now page polls status every 5 s
#: and the renders directory can hold a few thousand files on an SD card
STORAGE_TTL_SECONDS = 30.0
_storage_cache: dict[Path, tuple[float, dict[str, int]]] = {}
_storage_lock = threading.Lock()


def _tree_bytes(root: Path) -> int:
    """Total size of the regular files under ``root`` (0 if it is missing);
    entries that vanish mid-walk (a prune, a delete) are skipped."""
    total = 0
    stack = [root]
    while stack:
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total


def storage_usage(paths: DataPaths) -> dict[str, int]:
    """Bytes used by the masters, the render cache, the app caches and the
    database (with its WAL/SHM side files), reused for ``STORAGE_TTL_SECONDS``.

    Only called from the sync ``status`` route, which FastAPI runs in its
    threadpool (``anyio.to_thread.run_sync``), so the walk never blocks the
    event loop."""
    now = time.monotonic()
    with _storage_lock:
        hit = _storage_cache.get(paths.root)
        if hit is not None and now - hit[0] < STORAGE_TTL_SECONDS:
            return dict(hit[1])
    db_bytes = 0
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            db_bytes += Path(f"{paths.db_file}{suffix}").stat().st_size
        except OSError:
            pass
    usage = {
        "masters_bytes": _tree_bytes(paths.masters),
        "renders_bytes": _tree_bytes(paths.renders),
        "cache_bytes": _tree_bytes(paths.cache),
        "db_bytes": db_bytes,
    }
    with _storage_lock:
        _storage_cache[paths.root] = (now, usage)
    return dict(usage)


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
            "storage": storage_usage(paths),
        },
    }


@router.get("/system/logs")
def logs(
    n: Annotated[int, Query(ge=1, le=RING_BUFFER_LINES)] = 200,
) -> dict[str, list[str]]:
    """The last ``n`` log lines kept in memory (oldest first)."""
    return {"lines": get_recent_logs(n)}


def _terminate() -> None:
    """Signal this process to stop.  uvicorn turns SIGTERM into a clean
    shutdown (the scheduler finishes a refresh in flight and sleeps the
    panel) and systemd's ``Restart=always`` starts the unit again; on the
    Windows dev desktop ``os.kill`` simply ends the process."""
    log.warning("restart requested from the web UI; stopping the service")
    os.kill(os.getpid(), signal.SIGTERM)


@router.post("/system/restart")
def restart() -> dict[str, bool]:
    """Answer ``{"ok": true}``, then stop the process ``RESTART_DELAY_SECONDS``
    later; systemd starts it again."""
    timer = threading.Timer(RESTART_DELAY_SECONDS, _terminate)
    timer.daemon = True
    timer.start()
    return {"ok": True}
