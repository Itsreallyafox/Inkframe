"""JSON API routers and the request-scoped accessors they share.

The lifespan in ``inkframe.app`` puts the process-wide objects on
``app.state``; routers reach them through the ``*Dep`` annotations below
(``def route(db: DbDep): ...``) instead of importing globals.

Error convention for every route: non-2xx responses carry
``{"error": "<message>"}``.  Raise ``fastapi.HTTPException(status, detail)``
and the app converts it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from inkframe.config import SettingsStore
from inkframe.db import Database
from inkframe.display.base import Display
from inkframe.paths import DataPaths


def get_settings_store(request: Request) -> SettingsStore:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_display(request: Request) -> Display:
    return request.app.state.display


def get_paths(request: Request) -> DataPaths:
    return request.app.state.paths


SettingsDep = Annotated[SettingsStore, Depends(get_settings_store)]
DbDep = Annotated[Database, Depends(get_db)]
DisplayDep = Annotated[Display, Depends(get_display)]
PathsDep = Annotated[DataPaths, Depends(get_paths)]
