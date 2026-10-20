"""JSON API routers and the request-scoped accessors they share.

The lifespan in ``inkframe.app`` puts the process-wide objects on
``app.state``; routers reach them through the ``*Dep`` annotations below
(``def route(db: DbDep): ...``) instead of importing globals.

Error convention for every route: non-2xx responses carry
``{"error": "<message>"}``.  Raise ``fastapi.HTTPException(status, detail)``
and the app converts it.

Heavy work (image decode, resize, dither, PNG encode) never runs on the
event loop: ``async`` routes hand it to the one-thread render executor with
``run_on_render_executor``, so previews, uploads and the scheduler share one
worker and the Pi never runs two dithers at once.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import Executor
from typing import Annotated, Any, TypeVar

from fastapi import Depends, Request

from inkframe.config import SettingsStore
from inkframe.db import Database
from inkframe.display.base import Display
from inkframe.paths import DataPaths

T = TypeVar("T")


def get_settings_store(request: Request) -> SettingsStore:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_display(request: Request) -> Display:
    return request.app.state.display


def get_paths(request: Request) -> DataPaths:
    return request.app.state.paths


def get_render_executor(request: Request) -> Executor:
    return request.app.state.render_executor


SettingsDep = Annotated[SettingsStore, Depends(get_settings_store)]
DbDep = Annotated[Database, Depends(get_db)]
DisplayDep = Annotated[Display, Depends(get_display)]
PathsDep = Annotated[DataPaths, Depends(get_paths)]
RenderExecutorDep = Annotated[Executor, Depends(get_render_executor)]


async def run_on_render_executor(executor: Executor, fn: Callable[..., T], *args: Any) -> T:
    """Run ``fn(*args)`` on the render executor and await its result without
    blocking the event loop.  Exceptions raised by ``fn`` propagate."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, functools.partial(fn, *args))
