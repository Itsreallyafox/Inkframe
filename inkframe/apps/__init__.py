"""The apps the frame can cycle through (plan section 8).

Each app is a module ``inkframe.apps.<id>`` exposing a module-level ``APP``
object that satisfies ``inkframe.apps.base.App``.  The registry below is the
single list of ids the scheduler, the rotation settings and the API know;
adding an app means adding its id here and writing the module.
"""

from __future__ import annotations

import importlib

from inkframe.apps.base import App

#: every app id, in the order the Apps page lists them
APP_IDS: tuple[str, ...] = ("photos", "weather", "headlines", "patterns", "quotes")


def load_app(app_id: str) -> App:
    """Import ``inkframe.apps.<app_id>`` and return its ``APP``.

    Raises ``KeyError`` for an id that is not in ``APP_IDS`` and
    ``RuntimeError`` if the module exists but has no valid ``APP``.
    """
    if app_id not in APP_IDS:
        raise KeyError(f"unknown app {app_id!r}; known apps: {', '.join(APP_IDS)}")
    module = importlib.import_module(f"inkframe.apps.{app_id}")
    app = getattr(module, "APP", None)
    if not isinstance(app, App):
        raise RuntimeError(f"inkframe.apps.{app_id} has no APP object implementing App")
    if app.id != app_id:
        raise RuntimeError(f"inkframe.apps.{app_id}.APP.id is {app.id!r}, expected {app_id!r}")
    return app


def load_apps() -> dict[str, App]:
    """All apps, keyed by id, in ``APP_IDS`` order.  Fails loudly on a broken app."""
    return {app_id: load_app(app_id) for app_id in APP_IDS}
