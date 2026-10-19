"""JSON API routers and the request-scoped accessors they share.

The lifespan in ``inkframe.app`` puts the process-wide objects on
``app.state``; routers reach them through the ``*Dep`` annotations below
(``def route(db: DbDep): ...``) instead of importing globals.

Error convention for every route: non-2xx responses carry
``{"error": "<message>"}``.  Raise ``fastapi.HTTPException(status, detail)``
and the app converts it.
"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import Executor
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from inkframe.apps.base import App, FontLibrary
from inkframe.config import SettingsStore
from inkframe.db import Database
from inkframe.display.base import Display
from inkframe.paths import DataPaths
from inkframe.scheduler import Scheduler


def get_settings_store(request: Request) -> SettingsStore:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_display(request: Request) -> Display:
    return request.app.state.display


def get_paths(request: Request) -> DataPaths:
    return request.app.state.paths


def get_scheduler(request: Request) -> Scheduler:
    """The rotation scheduler; 503 if the app was built without one."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="the scheduler is not available")
    return scheduler


def get_apps(request: Request) -> Mapping[str, App]:
    """Every loaded app, keyed by id, in ``APP_IDS`` order."""
    return request.app.state.apps


def get_render_executor(request: Request) -> Executor:
    """The one-thread executor every render (previews, scheduler) runs on."""
    return request.app.state.render_executor


def get_fonts(request: Request) -> FontLibrary:
    """The process's shared ``FontLibrary`` (used on the render thread only)."""
    return request.app.state.fonts


SettingsDep = Annotated[SettingsStore, Depends(get_settings_store)]
DbDep = Annotated[Database, Depends(get_db)]
DisplayDep = Annotated[Display, Depends(get_display)]
PathsDep = Annotated[DataPaths, Depends(get_paths)]
SchedulerDep = Annotated[Scheduler, Depends(get_scheduler)]
AppsDep = Annotated[Mapping[str, App], Depends(get_apps)]
ExecutorDep = Annotated[Executor, Depends(get_render_executor)]
FontsDep = Annotated[FontLibrary, Depends(get_fonts)]
