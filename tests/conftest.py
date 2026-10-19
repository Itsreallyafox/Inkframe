"""Shared fixtures: every test gets its own throw-away data directory.

Also the scheduler test rig (``Rig``, ``FakeClock``, ``FakeApp``,
``RecordingDisplay``): nothing in it starts the scheduler thread or sleeps.
Tests call ``Rig.tick()`` (``Scheduler.tick(clock.now)``) and move the fake
clock by hand; the scheduler's monotonic clock follows the same fake wall
clock, so no clock jump is seen unless a test fakes one on purpose.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from inkframe.app import create_app
from inkframe.apps.base import RenderContext
from inkframe.config import SettingsStore
from inkframe.db import Database, Photo
from inkframe.display.base import DisplayTimeout
from inkframe.display.mock import MockDisplay
from inkframe.display.palette import PANEL_HEIGHT, PANEL_WIDTH
from inkframe.paths import DataPaths
from inkframe.scheduler import Scheduler


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the developer's INKFRAME_* environment leak into a test.

    Safety nets: no app built in a test starts the scheduler thread (tests
    that want it unset ``INKFRAME_SCHEDULER`` or start a ``Scheduler``
    themselves), and nothing rendered through the scheduler or the preview
    endpoint touches the network.
    """
    for name in (
        "INKFRAME_DATA",
        "INKFRAME_DISPLAY",
        "INKFRAME_HOST",
        "INKFRAME_PORT",
        "INKFRAME_SCHEDULER",
        "INKFRAME_OFFLINE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INKFRAME_SCHEDULER", "off")
    monkeypatch.setenv("INKFRAME_OFFLINE", "1")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def settings_store(data_dir: Path) -> SettingsStore:
    return SettingsStore(data_dir / "settings.json")


@pytest.fixture
def mock_display(data_dir: Path) -> MockDisplay:
    return MockDisplay(data_dir)


@pytest.fixture
def client(data_dir: Path, mock_display: MockDisplay) -> Iterator[TestClient]:
    """A running app (lifespan entered) on ``data_dir`` with ``mock_display``.

    The scheduler exists (``app.state.scheduler``) but its thread is not
    started; tests tick it by hand.
    """
    app = create_app(data_dir=data_dir, display=mock_display, scheduler=False)
    with TestClient(app) as c:
        yield c


def stripes_frame() -> np.ndarray:
    """A (400, 600) frame cycling through all six indices in 100-px columns."""
    cols = (np.arange(PANEL_WIDTH) // 100) % 6
    return np.tile(cols.astype(np.uint8), (PANEL_HEIGHT, 1))


# --- scheduler rig -------------------------------------------------------------------

#: a fixed local zone, so wall-clock maths in tests is unambiguous
TZ = timezone(timedelta(hours=-4), "EDT")
#: Monday 19 October 2026, 12:00 local
START = datetime(2026, 10, 19, 12, 0, tzinfo=TZ)


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Local time on ``2026-10-<day>`` (19 is a Monday, 25 a Sunday)."""
    return datetime(2026, 10, day, hour, minute, second, tzinfo=TZ)


class FakeClock:
    """A mutable "now"; ``monotonic()`` follows it exactly."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start
        self._origin = start

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return 1000.0 + (self.now - self._origin).total_seconds()

    def advance(self, **kw: float) -> datetime:
        self.now += timedelta(**kw)
        return self.now

    def set(self, t: datetime) -> datetime:
        assert t >= self.now, "the fake clock only moves forward"
        self.now = t
        return t


class FakeApp:
    """Paints the whole frame in one palette index (a photo id picks the
    colour for the photos stand-in) and records every render's item id."""

    needs_dither = False
    settings_schema: tuple = ()
    variants = 1

    def __init__(self, app_id: str, colour: int, *, fail: bool = False) -> None:
        self.id = app_id
        self.name = app_id.title()
        self.description = f"fake {app_id}"
        self.colour = colour
        self.fail = fail
        self.renders: list[str | None] = []

    def render(self, ctx: RenderContext) -> np.ndarray:
        self.renders.append(ctx.item_id)
        if self.fail:
            raise RuntimeError(f"{self.id} is broken")
        colour = self.colour if ctx.item_id is None else int(ctx.item_id[:8], 16) % 6
        frame = np.full((PANEL_HEIGHT, PANEL_WIDTH), colour, dtype=np.uint8)
        frame[0, 0] = self.colour
        return frame


def fake_apps(*failing: str) -> dict[str, FakeApp]:
    """photos/alpha/beta/gamma fakes; the ids in ``failing`` raise on render."""
    apps = {
        "photos": FakeApp("photos", 1),
        "alpha": FakeApp("alpha", 2),
        "beta": FakeApp("beta", 3),
        "gamma": FakeApp("gamma", 4),
    }
    for app_id in failing:
        apps[app_id].fail = True
    return apps


class RecordingDisplay(MockDisplay):
    """A MockDisplay that records the order of calls and can be told to fail."""

    def __init__(self, data_dir: Path, **kw: Any) -> None:
        super().__init__(data_dir, **kw)
        self.calls: list[str] = []
        self.fail_show = False
        self.fail_init = False

    def init(self) -> None:
        self.calls.append("init")
        if self.fail_init:
            raise OSError("SPI device went away")
        super().init()

    def clear(self) -> None:
        self.calls.append("clear")
        super().clear()

    def show(self, frame: np.ndarray) -> None:
        self.calls.append("show")
        if self.fail_show:
            raise DisplayTimeout("BUSY did not release")
        super().show(frame)

    def sleep(self) -> None:
        self.calls.append("sleep")
        super().sleep()


def slot(app_id: str, minutes: int, enabled: bool = True, **options: Any) -> dict[str, Any]:
    """A rotation slot dict for settings patches."""
    return {"app_id": app_id, "duration_minutes": minutes, "enabled": enabled, "options": options}


#: every rig starts from this: quiet hours off, three 1-minute slots
BASE_SETTINGS: dict[str, Any] = {
    "quiet_hours": {"enabled": False},
    "rotation": [slot("alpha", 1), slot("beta", 1), slot("gamma", 1)],
}


class Rig:
    """One data dir, settings store, db, display, fake clock and scheduler."""

    def __init__(
        self,
        root: Path,
        settings: Mapping[str, Any] | None = None,
        *,
        apps: Mapping[str, Any] | None = None,
        clock: FakeClock | None = None,
        display: RecordingDisplay | None = None,
    ) -> None:
        self.data = root / "data"
        self.paths = DataPaths(self.data).ensure_dirs()
        self.store = SettingsStore(self.paths.settings_file)
        self.store.patch(BASE_SETTINGS)
        if settings:
            self.store.patch(settings)
        self.db = Database(self.paths.db_file)
        self.display = display or RecordingDisplay(self.data)
        self.clock = clock or FakeClock()
        self.apps = dict(apps) if apps is not None else fake_apps()
        self.sched = self.new_scheduler()

    def new_scheduler(self, **kw: Any) -> Scheduler:
        """A scheduler over this rig's storage (call again to "restart")."""
        kw.setdefault("rng", random.Random(1234))
        kw.setdefault("now_fn", self.clock)
        kw.setdefault("monotonic_fn", self.clock.monotonic)
        return Scheduler(self.display, self.store, self.db, self.paths, self.apps, **kw)

    def tick(self) -> float:
        return self.sched.tick(self.clock())

    def snapshot(self) -> dict[str, Any]:
        return self.sched.snapshot()

    @property
    def shows(self) -> int:
        return self.display.show_count

    def source(self) -> dict[str, Any] | None:
        return self.snapshot()["panel"]["last_source"]

    def shown_id(self) -> str | None:
        src = self.source()
        return None if src is None else src["id"]

    def add_photo(self, name: str, created_at: str, *, excluded: bool = False) -> Photo:
        return self.db.add_photo(
            original_name=name,
            width=1800,
            height=1200,
            master_path=f"masters/{name}",
            master_bytes=1000,
            created_at=created_at,
            excluded=excluded,
        )

    def run_until(self, end: datetime) -> list[datetime]:
        """Follow the delays ``tick`` asks for until ``end``; returns the times
        at which the panel was refreshed (``show()`` called)."""
        refreshes = []
        while self.clock.now < end:
            before = self.shows
            delay = self.tick()
            if self.shows != before:
                refreshes.append(self.clock.now)
            self.clock.advance(seconds=delay)
        return refreshes

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    """``make_rig(settings_patch, apps=..., display=...)`` -> a fresh ``Rig``."""
    rigs: list[Rig] = []

    def factory(settings: Mapping[str, Any] | None = None, **kw: Any) -> Rig:
        rig = Rig(tmp_path / f"rig{len(rigs)}", settings, **kw)
        rigs.append(rig)
        return rig

    yield factory
    for rig in rigs:
        rig.close()
