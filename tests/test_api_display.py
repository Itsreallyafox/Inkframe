"""HTTP: /api/display/* (next, pause, resume, show), /api/rotation, and the
scheduler's part of /api/status.

The ``client`` fixture's app has a Scheduler whose thread is not running;
tests tick it by hand (``_tick``) with the real clock and the real apps,
rendered offline (``INKFRAME_OFFLINE=1`` from conftest).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import inkframe.app as app_module
import inkframe.scheduler as scheduler_module
from inkframe.app import create_app
from inkframe.config import SettingsStore
from inkframe.display.mock import MockDisplay
from inkframe.scheduler import Scheduler, iso, local_now
from tests.conftest import (
    FakeApp,
    FakeClock,
    RecordingDisplay,
    Rig,
    at,
    fake_apps,
    gradient_image,
    jpeg_bytes,
)

SNAPSHOT_KEYS = {"ok", "panel", "rotation"}
PANEL_KEYS = {"state", "last_refresh_at", "last_attempt_at", "last_source", "last_error"}
ROTATION_KEYS = {
    "paused",
    "quiet",
    "quiet_until",
    "current",
    "next",
    "next_at",
    "keepalive_at",
    "pending",
}


def _tick(client: TestClient) -> float:
    return client.app.state.scheduler.tick(local_now())


def _quiet_off(client: TestClient) -> None:
    r = client.put("/api/settings", json={"quiet_hours": {"enabled": False}})
    assert r.status_code == 200


def _add_photo(client: TestClient, name: str = "beach.jpg") -> str:
    """Upload a small real photo through the API (the photos app needs a master)."""
    data = jpeg_bytes(gradient_image(600, 400))
    r = client.post("/api/photos", files={"file": (name, data, "image/jpeg")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- /api/display ----------------------------------------------------------------------


def test_show_app_queues_then_refreshes(client: TestClient, mock_display: MockDisplay) -> None:
    r = client.post("/api/display/show", json={"app_id": "quotes"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == SNAPSHOT_KEYS and body["ok"] is True
    assert set(body["panel"]) == PANEL_KEYS and set(body["rotation"]) == ROTATION_KEYS
    pending = body["rotation"]["pending"]
    assert pending["kind"] == "app" and pending["id"] == "quotes" and pending["label"] == "Quotes"
    assert pending["fires_at"].endswith("+00:00")
    assert mock_display.show_count == 0  # queued, not shown: only the scheduler drives the panel

    _tick(client)
    assert mock_display.show_count == 1 and mock_display.awake is False
    status = client.get("/api/status").json()
    assert status["panel"]["last_source"] == {"type": "app", "id": "quotes", "label": "Quotes"}
    assert status["rotation"]["pending"] is None
    assert status["panel"]["last_refresh_at"] == status["panel"]["last_attempt_at"]


def test_show_photo_even_when_excluded(client: TestClient, mock_display: MockDisplay) -> None:
    photo_id = _add_photo(client)
    client.app.state.db.set_photo_excluded(photo_id, True)
    r = client.post("/api/display/show", json={"photo_id": photo_id})
    assert r.status_code == 200
    assert r.json()["rotation"]["pending"]["label"] == "beach.jpg"
    _tick(client)
    src = client.get("/api/status").json()["panel"]["last_source"]
    assert src == {"type": "photo", "id": photo_id, "label": "beach.jpg"}


@pytest.mark.parametrize(
    ("body", "status", "fragment"),
    [
        ({"app_id": "nope"}, 404, "unknown app"),
        ({"photo_id": "f" * 32}, 404, "unknown photo"),
        ({"app_id": "quotes", "photo_id": "f" * 32}, 422, "exactly one"),
        ({}, 422, "exactly one"),
        ({"app_id": None}, 422, "exactly one"),
        ({"app_id": "quotes", "photo_id": None}, 422, "exactly one"),
        ({"app_id": None, "photo_id": None}, 422, "exactly one"),
        ({"app_id": 3}, 422, "non-empty string"),
        ({"app_id": "  "}, 422, "non-empty string"),
        ({"app_id": "quotes", "colour": "red"}, 422, "unknown key"),
    ],
)
def test_show_rejects_bad_requests(
    client: TestClient, body: dict[str, Any], status: int, fragment: str
) -> None:
    r = client.post("/api/display/show", json=body)
    assert r.status_code == status
    assert fragment in r.json()["error"]
    assert client.get("/api/status").json()["rotation"]["pending"] is None


def test_show_needs_a_json_object(client: TestClient) -> None:
    assert client.post("/api/display/show", json=["quotes"]).status_code == 422
    assert client.post("/api/display/show").status_code == 422


def test_next_shows_the_upcoming_rotation_item(
    client: TestClient, mock_display: MockDisplay
) -> None:
    body = client.post("/api/display/next").json()
    assert body["ok"] is True
    upcoming = body["rotation"]["next"]
    assert upcoming == {"app_id": "photos", "label": "Photos", "photo_id": None}
    assert body["rotation"]["pending"]["kind"] == "next"
    assert body["rotation"]["pending"]["label"] == "Photos"
    _tick(client)
    assert mock_display.show_count == 1
    status = client.get("/api/status").json()
    assert status["panel"]["last_source"] == {"type": "app", "id": "photos", "label": "Photos"}
    assert status["rotation"]["current"]["app_id"] == "photos"
    assert status["rotation"]["next"]["app_id"] == "weather"


def test_pause_and_resume_persist(client: TestClient, data_dir: Path) -> None:
    body = client.post("/api/display/pause").json()
    assert body["ok"] is True and body["rotation"]["paused"] is True
    on_disk = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert on_disk["paused"] is True
    assert client.get("/api/status").json()["rotation"]["paused"] is True
    assert client.get("/api/rotation").json()["paused"] is True
    assert client.post("/api/display/pause").json()["rotation"]["paused"] is True  # idempotent

    body = client.post("/api/display/resume").json()
    assert body["rotation"]["paused"] is False
    on_disk = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert on_disk["paused"] is False


def test_paused_rotation_does_not_refresh(client: TestClient, mock_display: MockDisplay) -> None:
    _quiet_off(client)
    client.post("/api/display/pause")
    _tick(client)
    assert mock_display.show_count == 0
    client.post("/api/display/resume")
    _tick(client)
    assert mock_display.show_count == 1


def test_display_routes_without_scheduler_are_503(data_dir: Path) -> None:
    app = create_app(data_dir=data_dir, display=MockDisplay(data_dir), scheduler=False)
    with TestClient(app) as c:
        c.app.state.scheduler = None
        r = c.post("/api/display/next")
        assert r.status_code == 503 and "scheduler" in r.json()["error"]
        status = c.get("/api/status").json()  # the shape holds without one
        assert set(status["panel"]) == PANEL_KEYS and set(status["rotation"]) == ROTATION_KEYS
        assert status["rotation"]["current"] is None


# --- /api/status ------------------------------------------------------------------------


def test_status_is_filled_from_the_scheduler(client: TestClient) -> None:
    _quiet_off(client)
    _tick(client)  # photos slot, no photos: the "no photos" card
    s = client.get("/api/status").json()
    assert set(s) == {"version", "display", "panel", "rotation", "settings", "system"}
    assert set(s["panel"]) == PANEL_KEYS and set(s["rotation"]) == ROTATION_KEYS
    panel, rot = s["panel"], s["rotation"]
    assert panel["state"] == "asleep"
    assert panel["last_refresh_at"] is not None and panel["last_error"] is None
    assert panel["last_source"] == {"type": "app", "id": "photos", "label": "Photos"}
    assert rot["paused"] is False and rot["quiet"] is False and rot["quiet_until"] is None
    assert rot["current"]["app_id"] == "photos" and rot["current"]["photo_id"] is None
    assert set(rot["current"]) == {"app_id", "label", "photo_id", "started_at", "ends_at"}
    assert rot["next"] == {"app_id": "weather", "label": "Weather", "photo_id": None}
    assert rot["next_at"] > panel["last_refresh_at"]
    assert rot["keepalive_at"] > rot["next_at"]
    assert rot["pending"] is None
    # what the panel shows is on disk, in panel colours and controller bytes
    data_dir = Path(s["system"]["data_dir"])
    assert (data_dir / "last_frame.png").is_file()
    assert (data_dir / "last_frame.bin").stat().st_size == 120000


def test_status_survives_unreadable_state_rows(client: TestClient) -> None:
    """R7: a state row that is not JSON (torn write, SD-card bit rot) is
    reported as unknown, or as the scheduler's own value, never a 500; and a
    scheduler started on such rows boots with defaults."""
    _quiet_off(client)
    _tick(client)
    state = client.app.state
    db = state.db
    with db._lock:
        for key in ("scheduler", "last_refresh_at", "last_source"):
            db._conn.execute("UPDATE state SET value = ? WHERE key = ?", ("{torn", key))
    r = client.get("/api/status")
    assert r.status_code == 200, r.text
    panel = r.json()["panel"]
    assert panel["last_source"] == {"type": "app", "id": "photos", "label": "Photos"}
    assert panel["last_refresh_at"] == panel["last_attempt_at"] is not None
    sched = Scheduler(state.display, state.settings, db, state.paths, state.apps)
    assert sched.snapshot()["panel"]["last_refresh_at"] is None
    assert db.get_state("scheduler")["slot_index"] == 0  # rewritten as valid JSON


def _fake_clock_scheduler(client: TestClient, clock: FakeClock) -> Scheduler:
    """Replace the app's scheduler with one on a fake clock (same storage)."""
    state = client.app.state
    sched = Scheduler(
        state.display,
        state.settings,
        state.db,
        state.paths,
        state.apps,
        now_fn=clock,
        monotonic_fn=clock.monotonic,
        fonts=state.fonts,
    )
    state.scheduler = sched
    return sched


def test_status_reports_quiet_hours(client: TestClient, mock_display: MockDisplay) -> None:
    clock = FakeClock(at(19, 22, 59))
    sched = _fake_clock_scheduler(client, clock)
    sched.tick(clock())  # 22:59: the photos slot's "no photos" card
    assert mock_display.show_count == 1
    clock.set(at(19, 23, 30))
    rot = client.get("/api/status").json()["rotation"]
    assert rot["quiet"] is True and rot["quiet_until"] == iso(at(20, 7))
    assert rot["next_at"] == iso(at(20, 7))  # the rotation sleeps until the window ends
    assert rot["keepalive_at"] == iso(at(20, 18, 59))
    sched.tick(clock())
    assert mock_display.show_count == 1
    # show now still works in quiet hours
    body = client.post("/api/display/show", json={"app_id": "patterns"}).json()
    assert body["rotation"]["pending"]["fires_at"] == iso(at(19, 23, 30))
    sched.tick(clock())
    assert mock_display.show_count == 2
    assert client.get("/api/status").json()["panel"]["last_source"]["id"] == "patterns"


def test_settings_put_wakes_the_scheduler(client: TestClient, monkeypatch) -> None:
    woken = threading.Event()
    monkeypatch.setattr(client.app.state.scheduler, "wake", woken.set)
    client.put("/api/settings", json={"panel": {"refresh_floor_seconds": 240}})
    assert woken.is_set()
    woken.clear()
    client.put("/api/settings", json={"panel": {"refresh_floor_seconds": 1}})  # 422
    assert not woken.is_set()


def test_lifespan_starts_and_stops_the_scheduler_thread(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INKFRAME_SCHEDULER")
    display = MockDisplay(data_dir)
    app = create_app(data_dir=data_dir, display=display)
    with TestClient(app):
        sched = app.state.scheduler
        assert sched.running
        assert any(t.name == "scheduler" for t in threading.enumerate())
    assert not sched.running
    assert display.awake is False


def test_shutdown_leaves_a_refresh_in_flight_to_the_scheduler(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the stop timeout runs out mid-refresh, the lifespan does not put the
    display to sleep itself: the driver's lock is free between its calls, so
    a sleep() there would make the refresh's show() fail on a sleeping panel
    (left white after a clear()).  The refresh's own ``finally`` sleeps it."""
    monkeypatch.delenv("INKFRAME_SCHEDULER")
    monkeypatch.setattr(app_module, "SCHEDULER_STOP_TIMEOUT", 0.2)
    SettingsStore(data_dir / "settings.json").patch({"quiet_hours": {"enabled": False}})
    entered, gate = threading.Event(), threading.Event()

    class SlowShow(RecordingDisplay):
        def show(self, frame: Any) -> None:
            entered.set()
            gate.wait(20)
            super().show(frame)

    display = SlowShow(data_dir)
    app = create_app(data_dir=data_dir, display=display)
    try:
        with TestClient(app):
            sched = app.state.scheduler
            assert entered.wait(20)
        thread = sched._thread
        assert thread is not None and thread.is_alive() and sched.refreshing
        assert display.calls == ["init"]  # the lifespan left the panel alone
    finally:
        gate.set()
    thread.join(10)
    assert not thread.is_alive()
    assert display.calls == ["init", "show", "sleep"]
    assert display.show_count == 1 and display.awake is False


def test_scheduler_env_off_keeps_the_thread_stopped(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INKFRAME_SCHEDULER", "off")
    app = create_app(data_dir=data_dir, display=MockDisplay(data_dir))
    with TestClient(app):
        assert not app.state.scheduler.running


# --- /api/rotation ------------------------------------------------------------------------


def test_rotation_get(client: TestClient) -> None:
    r = client.get("/api/rotation")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"slots", "paused", "quiet_hours", "apps"}
    assert body["apps"] == ["photos", "weather", "headlines", "patterns", "quotes"]
    assert [s["app_id"] for s in body["slots"]] == [
        "photos",
        "weather",
        "headlines",
        "quotes",
        "patterns",
    ]
    photos = body["slots"][0]
    assert photos == {
        "app_id": "photos",
        "enabled": True,
        "duration_minutes": 10,
        "options": {"dwell_minutes": 3, "order": "shuffle"},
        "name": "Photos",
        "description": "Your uploaded photos",
    }
    assert body["slots"][1]["name"] == "Weather" and body["slots"][1]["options"] == {}
    assert body["quiet_hours"]["start"] == "23:00" and body["paused"] is False


def test_rotation_put_replaces_the_list(client: TestClient, monkeypatch) -> None:
    woken = threading.Event()
    monkeypatch.setattr(client.app.state.scheduler, "wake", woken.set)
    slots = [
        {"app_id": "quotes", "enabled": True, "duration_minutes": 5},
        {"app_id": "photos", "duration_minutes": 20, "options": {"dwell_minutes": 4}},
        {"app_id": "weather", "enabled": False, "duration_minutes": 3, "options": {}},
    ]
    r = client.put("/api/rotation", json={"slots": slots, "paused": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["app_id"] for s in body["slots"]] == ["quotes", "photos", "weather"]
    assert body["slots"][1]["options"] == {"dwell_minutes": 4, "order": "shuffle"}
    assert body["slots"][2]["enabled"] is False and body["paused"] is True
    assert woken.is_set()
    saved = client.get("/api/settings").json()
    assert saved["paused"] is True
    assert saved["rotation"][1] == {
        "app_id": "photos",
        "enabled": True,
        "duration_minutes": 20,
        "options": {"dwell_minutes": 4, "order": "shuffle"},
    }
    assert client.get("/api/rotation").json() == body


def test_rotation_put_round_trips_the_get_shape(client: TestClient) -> None:
    body = client.get("/api/rotation").json()
    body["slots"].reverse()
    r = client.put("/api/rotation", json={"slots": body["slots"]})
    assert r.status_code == 200, r.text
    assert r.json()["slots"] == body["slots"]


def test_rotation_get_round_trips_after_unknown_photos_options(client: TestClient) -> None:
    """``/api/settings`` stores any slot options; ``GET /api/rotation`` reports
    only the photos options the scheduler reads, so its body can be PUT back
    (an unknown key used to come back and make the PUT a 422)."""
    rotation = client.get("/api/settings").json()["rotation"]
    rotation[0]["options"]["zoom"] = 2
    r = client.put("/api/settings", json={"rotation": rotation})
    assert r.status_code == 200, r.text
    body = client.get("/api/rotation").json()
    assert body["slots"][0]["options"] == {"dwell_minutes": 3, "order": "shuffle"}
    r = client.put("/api/rotation", json={"slots": body["slots"]})
    assert r.status_code == 200, r.text
    assert r.json()["slots"] == body["slots"]


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"slots": [{"app_id": "radio", "duration_minutes": 5}]}, "unknown app"),
        ({"slots": [{"duration_minutes": 5}]}, "unknown app"),
        (
            {
                "slots": [
                    {"app_id": "photos", "duration_minutes": 5, "options": {"dwell_minutes": 0}}
                ]
            },
            "dwell_minutes",
        ),
        (
            {
                "slots": [
                    {"app_id": "photos", "duration_minutes": 5, "options": {"dwell_minutes": 1441}}
                ]
            },
            "dwell_minutes",
        ),
        (
            {
                "slots": [
                    {"app_id": "photos", "duration_minutes": 5, "options": {"dwell_minutes": 2.5}}
                ]
            },
            "dwell_minutes",
        ),
        (
            {
                "slots": [
                    {"app_id": "photos", "duration_minutes": 5, "options": {"order": "random"}}
                ]
            },
            "order",
        ),
        (
            {"slots": [{"app_id": "photos", "duration_minutes": 5, "options": {"zoom": 2}}]},
            "unknown key",
        ),
        ({"slots": [{"app_id": "weather", "duration_minutes": 0}]}, "duration_minutes"),
        ({"slots": [{"app_id": "weather", "duration_minutes": 3, "colour": 1}]}, "unknown key"),
        ({"slots": [{"app_id": "weather", "duration_minutes": 3, "options": [1]}]}, "options"),
        ({"slots": ["weather"]}, "must be an object"),
        ({"slots": "weather"}, "must be a list"),
        ({"slots": [], "paused": "yes"}, "paused"),
        ({"paused": True}, "slots is required"),
        ({"slots": [], "extra": 1}, "unknown key"),
    ],
)
def test_rotation_put_validation(client: TestClient, body: dict[str, Any], fragment: str) -> None:
    before = client.get("/api/settings").json()
    r = client.put("/api/rotation", json=body)
    assert r.status_code == 422, r.text
    assert fragment in r.json()["error"]
    assert client.get("/api/settings").json() == before


def test_rotation_put_empty_list_is_allowed(client: TestClient) -> None:
    r = client.put("/api/rotation", json={"slots": []})
    assert r.status_code == 200 and r.json()["slots"] == []
    rot = client.get("/api/status").json()["rotation"]
    assert rot["current"] is None and rot["next"] is None and rot["next_at"] is None


# --- /api/display/frame.png ------------------------------------------------------------


def test_frame_png_is_404_until_something_was_shown(client: TestClient) -> None:
    r = client.get("/api/display/frame.png")
    assert r.status_code == 404 and "error" in r.json()


def test_frame_png_serves_the_last_frame_with_an_etag(
    client: TestClient, data_dir: Path, mock_display: MockDisplay
) -> None:
    client.post("/api/display/show", json={"app_id": "quotes"})
    _tick(client)
    r = client.get("/api/display/frame.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-cache"
    assert r.content == (data_dir / "last_frame.png").read_bytes()
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    etag = r.headers["etag"]
    again = client.get("/api/display/frame.png", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["etag"] == etag
    stale = client.get("/api/display/frame.png", headers={"If-None-Match": '"0-0"'})
    assert stale.status_code == 200 and stale.content == r.content


# --- show now with a forced variant ----------------------------------------------------


class _VariantApp(FakeApp):
    """A fake app with three layouts that records the variant of each render."""

    variants = 3

    def __init__(self, app_id: str, colour: int) -> None:
        super().__init__(app_id, colour)
        self.variants_seen: list[int | None] = []

    def render(self, ctx: Any) -> Any:
        self.variants_seen.append(ctx.variant)
        return super().render(ctx)


def test_scheduler_show_now_passes_the_variant_to_the_render(tmp_path: Path) -> None:
    apps = fake_apps()
    alpha = apps["alpha"] = _VariantApp("alpha", 2)
    rig = Rig(tmp_path, apps=apps)
    info = rig.sched.show_now(app_id="alpha", variant=2)
    assert info["kind"] == "app" and info["id"] == "alpha"
    rig.tick()
    assert alpha.variants_seen == [2]
    # the Now page names a forced layout (the patterns test card) by it
    assert rig.source() == {"type": "app", "id": "alpha", "label": "Alpha", "variant": 2}
    # the variant was for that one showing: a plain show-now picks its own
    rig.sched.show_now(app_id="alpha")
    rig.clock.advance(seconds=200)
    rig.tick()
    assert alpha.variants_seen == [2, None]
    assert rig.source() == {"type": "app", "id": "alpha", "label": "Alpha"}


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"app_id": "alpha", "variant": 3}, "0..2"),
        ({"app_id": "alpha", "variant": -1}, "0..2"),
        ({"app_id": "beta", "variant": 1}, "0..0"),
        ({"app_id": "alpha", "variant": True}, "whole number"),
        ({"photo_id": "ab" * 16, "variant": 0}, "app_id only"),
    ],
)
def test_scheduler_show_now_rejects_bad_variants(
    tmp_path: Path, kwargs: dict[str, Any], fragment: str
) -> None:
    apps = fake_apps()
    apps["alpha"] = _VariantApp("alpha", 2)
    rig = Rig(tmp_path, apps=apps)
    with pytest.raises(ValueError, match=fragment):
        rig.sched.show_now(**kwargs)
    assert rig.snapshot()["rotation"]["pending"] is None


def test_show_patterns_test_card_variant_over_http(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, int | None]] = []
    real = scheduler_module.build_render_context

    def spy(app: Any, **kw: Any) -> Any:
        seen.append((app.id, kw.get("variant")))
        return real(app, **kw)

    monkeypatch.setattr(scheduler_module, "build_render_context", spy)
    before = client.get("/api/apps/patterns/settings").json()
    r = client.post("/api/display/show", json={"app_id": "patterns", "variant": 2})
    assert r.status_code == 200, r.text
    assert r.json()["rotation"]["pending"]["id"] == "patterns"
    _tick(client)
    assert seen == [("patterns", 2)]
    status = client.get("/api/status").json()
    assert status["panel"]["last_source"]["id"] == "patterns"
    # the user's pattern settings are untouched
    assert client.get("/api/apps/patterns/settings").json() == before


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"app_id": "patterns", "variant": 3}, "0..2"),
        ({"app_id": "patterns", "variant": "2"}, "whole number"),
        ({"app_id": "patterns", "variant": True}, "whole number"),
        ({"app_id": "patterns", "variant": None}, "whole number"),
        ({"app_id": "patterns", "variant": -1}, "whole number"),
        ({"photo_id": "ab" * 16, "variant": 0}, "app_id only"),
    ],
)
def test_show_variant_validation_over_http(
    client: TestClient, body: dict[str, Any], fragment: str
) -> None:
    r = client.post("/api/display/show", json=body)
    assert r.status_code == 422, r.text
    assert fragment in r.json()["error"]
    assert client.get("/api/status").json()["rotation"]["pending"] is None
