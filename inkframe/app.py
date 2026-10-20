"""FastAPI application factory and the process lifespan.

``create_app()`` is what uvicorn calls (``factory=True``).  The lifespan owns
every process-wide resource and puts it on ``app.state``:

=====================  ==========================================================
``paths``              ``DataPaths`` of the data directory
``settings``           ``SettingsStore``
``db``                 ``Database``
``display``            the ``Display`` (asleep until something refreshes it)
``display_env``        ``"mock"`` or ``"epd"`` (``INKFRAME_DISPLAY``)
``render_executor``    one-thread ``ThreadPoolExecutor`` for dithering/previews
``started_at``         UTC ``datetime`` of start-up
``started_monotonic``  ``time.monotonic()`` at start-up (for uptime)
``apps``               ``{app_id: App}`` of every app that loaded
``fonts``              the shared ``FontLibrary`` (used on the render thread)
``scheduler``          the ``Scheduler``, the only thing that drives the panel
=====================  ==========================================================

The scheduler's thread is started unless ``create_app(scheduler=False)`` or
``INKFRAME_SCHEDULER=off``; the ``Scheduler`` object exists either way, so the
display API and ``/api/status`` work (tests tick it by hand).

On shutdown the scheduler is stopped first (a refresh in flight finishes and
puts the panel to sleep), then the display is closed (``close()`` is
``sleep()`` plus releasing the GPIO lines), unless a refresh is somehow still
running after the stop timeout, the executor stopped and the database closed.
There is no auth: Inkframe is LAN-only by design.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from os import PathLike, stat_result
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from inkframe import __version__
from inkframe.api import apps as apps_api
from inkframe.api import display as display_api
from inkframe.api import photos as photos_api
from inkframe.api import recipe as recipe_api
from inkframe.api import rotation as rotation_api
from inkframe.api import settings as settings_api
from inkframe.api import system as system_api
from inkframe.apps import APP_IDS, load_app
from inkframe.apps.base import FONTS_DIR, App, FontLibrary
from inkframe.config import SettingsStore
from inkframe.db import Database
from inkframe.display.base import Display
from inkframe.display.factory import create_display
from inkframe.log import configure_logging
from inkframe.paths import STATIC_DIR, DataPaths, resolve_data_dir
from inkframe.scheduler import Scheduler

log = logging.getLogger("inkframe.app")

DISPLAY_ENV_VAR = "INKFRAME_DISPLAY"
#: ``INKFRAME_SCHEDULER=off`` keeps the scheduler thread from starting
SCHEDULER_ENV_VAR = "INKFRAME_SCHEDULER"
#: how long shutdown waits for a refresh in flight (a real one takes ~19 s,
#: about 40 s with clear_before_refresh, longer on a cold panel); the systemd
#: unit's TimeoutStopSec (150 s) must stay well above it
SCHEDULER_STOP_TIMEOUT = 100.0
_NO_CACHE = "no-cache"
_LONG_CACHE = "public, max-age=31536000"


def scheduler_enabled_by_env() -> bool:
    """False when ``INKFRAME_SCHEDULER`` is ``off`` (or 0/false/no)."""
    value = os.environ.get(SCHEDULER_ENV_VAR, "on").strip().lower()
    return value not in ("off", "0", "false", "no")


def load_available_apps() -> dict[str, App]:
    """Every app in ``APP_IDS`` that imports; a broken one is logged and left
    out (its rotation slots are skipped) instead of keeping the frame, and with
    it the keep-alive, from starting."""
    apps: dict[str, App] = {}
    for app_id in APP_IDS:
        try:
            apps[app_id] = load_app(app_id)
        except Exception:
            log.exception("app %s failed to load; its rotation slots will be skipped", app_id)
    return apps


def create_app(
    *,
    data_dir: Path | None = None,
    display: Display | None = None,
    display_kind: str | None = None,
    scheduler: bool = True,
) -> FastAPI:
    """Build the app.

    ``data_dir`` defaults to ``$INKFRAME_DATA`` or ``<repo>/data``.  ``display``
    injects a ready display (tests); otherwise a display of ``display_kind``
    (default ``$INKFRAME_DISPLAY``, else ``mock``) is created at start-up.
    ``scheduler=False`` (or ``INKFRAME_SCHEDULER=off``) builds the scheduler
    but does not start its thread.
    """
    paths = DataPaths(resolve_data_dir(data_dir))
    if display_kind is None:
        if display is not None:
            display_kind = display.kind
        else:
            display_kind = os.environ.get(DISPLAY_ENV_VAR, "").strip() or "mock"
    kind = display_kind.strip().lower()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        paths.ensure_dirs()
        store = SettingsStore(paths.settings_file)
        db = Database(paths.db_file)
        try:
            disp = display or create_display(kind, data_dir=paths.root, settings_store=store)
        except BaseException:
            db.close()
            raise
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render")
        apps = load_available_apps()
        fonts = FontLibrary()
        try:
            sched = Scheduler(disp, store, db, paths, apps, fonts=fonts, executor=executor)
        except BaseException:
            executor.shutdown(wait=False, cancel_futures=True)
            db.close()
            raise
        state = app.state
        state.paths = paths
        state.settings = store
        state.db = db
        state.display = disp
        state.display_env = kind
        state.render_executor = executor
        state.started_at = datetime.now(timezone.utc)
        state.started_monotonic = time.monotonic()
        state.apps = apps
        state.fonts = fonts
        state.scheduler = sched
        log.info("Inkframe %s up: data %s, display %s", __version__, paths.root, disp.kind)
        if scheduler and scheduler_enabled_by_env():
            sched.start()
        else:
            log.info(
                "scheduler thread not started (%s)",
                "create_app(scheduler=False)" if not scheduler else f"{SCHEDULER_ENV_VAR}=off",
            )
        try:
            yield
        finally:
            log.info("Inkframe shutting down")
            try:
                # before the display: a refresh in flight finishes and sleeps the panel
                sched.stop(timeout=SCHEDULER_STOP_TIMEOUT)
            except Exception:
                log.exception("stopping the scheduler failed during shutdown")
            if sched.refreshing:
                # the thread outlived the timeout mid-refresh.  The driver's
                # lock is free between init/clear/show, so sleep() here could
                # slip in after clear() and leave the panel white; the
                # refresh's own ``finally`` puts the panel to sleep, and the
                # unit's ExecStopPost drives the pins low after exit anyway
                log.warning("a panel refresh is still running; leaving the display to it")
            else:
                try:
                    # close() is sleep() plus releasing the GPIO lines
                    close = getattr(disp, "close", None)
                    if callable(close):
                        close()
                    else:
                        disp.sleep()
                except Exception:
                    log.exception("putting the display to sleep failed during shutdown")
            executor.shutdown(wait=False, cancel_futures=True)
            db.close()

    app = FastAPI(title="Inkframe", version=__version__, lifespan=lifespan)
    app.add_middleware(_CatchAllMiddleware)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.include_router(system_api.router)
    app.include_router(settings_api.router)
    app.include_router(display_api.router)
    app.include_router(rotation_api.router)
    app.include_router(apps_api.router)
    app.include_router(photos_api.router)
    app.include_router(recipe_api.router)
    app.mount("/static", _RevalidatingStaticFiles(directory=STATIC_DIR), name="static")
    # the bundled OFL fonts, for the web UI's display headings
    app.mount("/fonts", _LongCacheStaticFiles(directory=FONTS_DIR), name="fonts")

    # HEAD too: FastAPI's @get, unlike a plain Starlette Route, does not add it
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": _NO_CACHE})

    return app


class _RevalidatingStaticFiles(StaticFiles):
    """``StaticFiles`` that makes browsers revalidate on every load (cheap 304s
    on the LAN), so a deploy shows up without hashed file names or a build."""

    def file_response(
        self,
        full_path: PathLike[str] | str,
        stat_result: stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = _NO_CACHE
        return response


class _LongCacheStaticFiles(StaticFiles):
    """``StaticFiles`` for the bundled fonts: they never change in place (a new
    font would get a new file name), so browsers may keep them for a year."""

    def file_response(
        self,
        full_path: PathLike[str] | str,
        stat_result: stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = _LONG_CACHE
        return response


class _CatchAllMiddleware:
    """Turns any unhandled exception into a logged ``500 {"error": ...}``.

    This is the app's global exception handler.  It is a pure ASGI middleware
    rather than an ``Exception`` handler because Starlette re-raises after
    calling such a handler, which makes uvicorn log every traceback twice.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            log.exception("unhandled error in %s %s", scope.get("method"), scope.get("path"))
            if response_started:
                raise
            body = {"error": str(exc) or type(exc).__name__}
            await JSONResponse(body, status_code=500)(scope, receive, send)


async def _http_error(request: Request, exc: Exception) -> Response:
    """``HTTPException`` (ours, FastAPI's, StaticFiles' 404) -> ``{"error": detail}``."""
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code in (204, 304):
        return Response(status_code=exc.status_code, headers=exc.headers)
    return JSONResponse(
        {"error": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers
    )


async def _validation_error(request: Request, exc: Exception) -> Response:
    """Request parsing failures -> ``422 {"error": "body.x: reason; ..."}``."""
    assert isinstance(exc, RequestValidationError)
    parts = []
    for err in exc.errors()[:3]:
        where = ".".join(str(p) for p in err.get("loc", ()))
        msg = err.get("msg", "invalid value")
        parts.append(f"{where}: {msg}" if where else str(msg))
    return JSONResponse({"error": "; ".join(parts) or "invalid request"}, status_code=422)
