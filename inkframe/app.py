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
``scheduler``          ``None`` until Phase 6 adds the rotation scheduler
=====================  ==========================================================

On shutdown the display is put to sleep (and closed, if it has ``close()``),
the executor stopped and the database closed.  There is no auth: Inkframe is LAN-only by design.
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
from inkframe.api import settings as settings_api
from inkframe.api import system as system_api
from inkframe.config import SettingsStore
from inkframe.db import Database
from inkframe.display.base import Display
from inkframe.display.factory import create_display
from inkframe.log import configure_logging
from inkframe.paths import STATIC_DIR, DataPaths, resolve_data_dir

log = logging.getLogger("inkframe.app")

DISPLAY_ENV_VAR = "INKFRAME_DISPLAY"
_NO_CACHE = "no-cache"


def create_app(
    *,
    data_dir: Path | None = None,
    display: Display | None = None,
    display_kind: str | None = None,
) -> FastAPI:
    """Build the app.

    ``data_dir`` defaults to ``$INKFRAME_DATA`` or ``<repo>/data``.  ``display``
    injects a ready display (tests); otherwise a display of ``display_kind``
    (default ``$INKFRAME_DISPLAY``, else ``mock``) is created at start-up.
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
        state = app.state
        state.paths = paths
        state.settings = store
        state.db = db
        state.display = disp
        state.display_env = kind
        state.render_executor = executor
        state.started_at = datetime.now(timezone.utc)
        state.started_monotonic = time.monotonic()
        state.scheduler = None
        log.info("Inkframe %s up: data %s, display %s", __version__, paths.root, disp.kind)
        try:
            yield
        finally:
            log.info("Inkframe shutting down")
            try:
                disp.sleep()
                # the epd driver also releases its GPIO lines; not part of the protocol
                close = getattr(disp, "close", None)
                if callable(close):
                    close()
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
    app.mount("/static", _RevalidatingStaticFiles(directory=STATIC_DIR), name="static")

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
