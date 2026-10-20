"""HTTP API: health, status shape, settings GET/PUT, index page, logs."""

from __future__ import annotations

import json
import logging
import sys
import warnings
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from inkframe.app import create_app
from inkframe.config import SettingsStore
from inkframe.display import factory
from inkframe.display.base import DisplayError
from inkframe.display.mock import MockDisplay, read_last_frame
from tests.conftest import stripes_frame


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"ok": True}
    r = client.head("/api/health")
    assert r.status_code == 200 and r.headers["content-type"] == "application/json"


def test_openapi_schema_builds_without_warnings(data_dir: Path) -> None:
    # e.g. "Duplicate Operation ID" from a route registered for both GET and HEAD
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        schema = create_app(data_dir=data_dir).openapi()
    assert list(schema["paths"]["/api/health"]) == ["get"]


def test_status_shape(client: TestClient, data_dir: Path) -> None:
    r = client.get("/api/status")
    assert r.status_code == 200
    s = r.json()
    assert set(s) == {"version", "display", "panel", "rotation", "settings", "system"}
    assert isinstance(s["version"], str)
    assert s["display"] == {"kind": "mock", "width": 600, "height": 400, "awake": False}
    assert set(s["panel"]) == {"state", "last_refresh_at", "last_source"}
    assert s["panel"] == {"state": "asleep", "last_refresh_at": None, "last_source": None}
    assert set(s["rotation"]) == {"paused", "current", "next", "next_at", "keepalive_at"}
    assert s["rotation"]["paused"] is False
    assert set(s["settings"]) == {
        "refresh_floor_seconds",
        "keepalive_hours",
        "quiet_hours",
        "clear_before_refresh",
    }
    assert s["settings"]["refresh_floor_seconds"] == 180
    assert s["settings"]["quiet_hours"] == {
        "enabled": True,
        "start": "23:00",
        "end": "07:00",
        "days": [0, 1, 2, 3, 4, 5, 6],
    }
    assert set(s["system"]) == {
        "uptime_seconds",
        "hostname",
        "data_dir",
        "disk_free_bytes",
        "display_env",
    }
    sysinfo = s["system"]
    assert isinstance(sysinfo["uptime_seconds"], float) and sysinfo["uptime_seconds"] >= 0
    assert isinstance(sysinfo["hostname"], str) and sysinfo["hostname"]
    assert Path(sysinfo["data_dir"]) == data_dir.resolve()
    assert isinstance(sysinfo["disk_free_bytes"], int) and sysinfo["disk_free_bytes"] > 0
    assert sysinfo["display_env"] == "mock"


def test_status_reflects_display_and_state(client: TestClient, mock_display: MockDisplay) -> None:
    db = client.app.state.db
    db.set_state("last_refresh_at", "2026-10-19T10:00:00.000+00:00")
    db.set_state("last_source", {"type": "photo", "id": "abc", "label": "Beach"})
    mock_display.init()
    s = client.get("/api/status").json()
    assert s["display"]["awake"] is True
    assert s["panel"]["state"] == "awake"
    assert s["panel"]["last_refresh_at"] == "2026-10-19T10:00:00.000+00:00"
    assert s["panel"]["last_source"] == {"type": "photo", "id": "abc", "label": "Beach"}
    client.put("/api/settings", json={"paused": True})
    assert client.get("/api/status").json()["rotation"]["paused"] is True


def test_settings_get(client: TestClient, data_dir: Path) -> None:
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["panel"]["refresh_floor_seconds"] == 180
    assert body == json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))


def test_settings_partial_put_merges(client: TestClient, data_dir: Path) -> None:
    r = client.put(
        "/api/settings",
        json={
            "panel": {"refresh_floor_seconds": 300},
            "palette": {"measured": {"blue": [1, 2, 3]}},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["panel"] == {
        "refresh_floor_seconds": 300,
        "show_now_min_seconds": 30,
        "keepalive_hours": 20,
    }
    assert body["palette"]["measured"]["blue"] == [1, 2, 3]
    assert body["palette"]["measured"]["red"] == [186, 48, 40]
    assert client.get("/api/settings").json() == body
    on_disk = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert on_disk == body


def test_settings_full_put_round_trips(client: TestClient) -> None:
    current = client.get("/api/settings").json()
    current["rotation"] = current["rotation"][:2]
    current["quiet_hours"]["days"] = [5, 6]
    r = client.put("/api/settings", json=current)
    assert r.status_code == 200
    assert r.json() == current


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"panel": {"refresh_floor_seconds": 10}}, "panel.refresh_floor_seconds"),
        ({"quiet_hours": {"start": "noon"}}, "quiet_hours.start"),
        ({"default_recipe": {"strength": 3}}, "default_recipe.strength"),
        ({"unknown_section": {}}, "unknown key"),
        ({"panel": {"refreshfloor": 300}}, "unknown key"),
    ],
)
def test_settings_put_invalid_is_422(client: TestClient, body: dict, fragment: str) -> None:
    before = client.get("/api/settings").json()
    r = client.put("/api/settings", json=body)
    assert r.status_code == 422
    assert fragment in r.json()["error"]
    assert client.get("/api/settings").json() == before


def test_settings_put_huge_integer_is_422(client: TestClient) -> None:
    before = client.get("/api/settings").json()
    body = '{"default_recipe": {"strength": 1' + "0" * 400 + "}}"  # float() overflows
    r = client.put("/api/settings", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 422 and "default_recipe.strength" in r.json()["error"]
    assert client.get("/api/settings").json() == before


def test_settings_put_non_object_is_422(client: TestClient) -> None:
    r = client.put("/api/settings", json=[1, 2, 3])
    assert r.status_code == 422
    assert "error" in r.json()
    r = client.put("/api/settings", content=b"{nope", headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_index_serves_html(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"
    assert "<title>Inkframe</title>" in r.text
    h = client.head("/")
    assert h.status_code == 200 and h.content == b""
    assert h.headers["content-type"].startswith("text/html")
    assert h.headers["cache-control"] == "no-cache"
    for asset, kind in (("/static/app.js", "javascript"), ("/static/app.css", "text/css")):
        a = client.get(asset)
        assert a.status_code == 200 and kind in a.headers["content-type"]
        assert a.headers["cache-control"] == "no-cache"


def test_unknown_route_uses_error_shape(client: TestClient) -> None:
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json() == {"error": "Not Found"}
    assert client.get("/static/nope.js").status_code == 404


def test_logs_endpoint(client: TestClient) -> None:
    logging.getLogger("inkframe.test").info("hello from the api test")
    r = client.get("/api/system/logs", params={"n": 50})
    assert r.status_code == 200
    lines = r.json()["lines"]
    assert isinstance(lines, list) and len(lines) <= 50
    assert any("inkframe.test: hello from the api test" in line for line in lines)
    assert client.get("/api/system/logs", params={"n": 0}).status_code == 422


def test_unhandled_error_is_logged_500(data_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(data_dir=data_dir, display=MockDisplay(data_dir))
    boom = APIRouter()

    @boom.get("/api/boom")
    def explode() -> None:
        raise RuntimeError("kaboom")

    app.include_router(boom)
    with TestClient(app) as c, caplog.at_level("ERROR", logger="inkframe.app"):
        r = c.get("/api/boom")
    assert r.status_code == 500 and r.json() == {"error": "kaboom"}
    assert any(r.exc_info for r in caplog.records if r.name == "inkframe.app")


def test_lifespan_state_and_shutdown(data_dir: Path) -> None:
    display = MockDisplay(data_dir)
    app = create_app(data_dir=data_dir, display=display)
    with TestClient(app):
        state = app.state
        assert state.display is display and state.scheduler is None
        assert state.render_executor.submit(lambda: 41 + 1).result(timeout=5) == 42
        assert isinstance(state.started_at, datetime) and state.started_at.tzinfo is not None
        for sub in ("masters", "renders", "cache"):
            assert (data_dir / sub).is_dir()
        display.init()
    assert display.awake is False  # shutdown put the panel to sleep
    assert (data_dir / "inkframe.db").exists()


def test_create_display_from_kind(data_dir: Path) -> None:
    app = create_app(data_dir=data_dir, display_kind="mock")
    with TestClient(app) as c:
        assert isinstance(app.state.display, MockDisplay)
        assert c.get("/api/status").json()["system"]["display_env"] == "mock"
        # the mock follows the settings store for rotation and palette
        c.put("/api/settings", json={"display": {"rotation": 270}})
        frame = stripes_frame()
        app.state.display.init()
        app.state.display.show(frame)
        assert (read_last_frame(data_dir, 270) == frame).all()


def test_display_env_var(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INKFRAME_DISPLAY", "bogus")
    app = create_app(data_dir=data_dir)
    with pytest.raises(ValueError, match="bogus"), TestClient(app):
        pass


def test_data_dir_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("INKFRAME_DATA", str(target))
    app = create_app()
    with TestClient(app) as c:
        assert Path(c.get("/api/status").json()["system"]["data_dir"]) == target.resolve()
    assert (target / "settings.json").exists()


def test_factory_epd_import_failure(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, settings_store: SettingsStore
) -> None:
    # a None entry makes "import inkframe.display.epd4in0e" raise ImportError
    monkeypatch.setitem(sys.modules, "inkframe.display.epd4in0e", None)
    with pytest.raises(DisplayError, match="only works on the Raspberry Pi"):
        factory.create_display("epd", data_dir=data_dir, settings_store=settings_store)


def test_factory_unknown_kind(data_dir: Path, settings_store: SettingsStore) -> None:
    with pytest.raises(ValueError, match="unknown display kind"):
        factory.create_display("lcd", data_dir=data_dir, settings_store=settings_store)
    assert isinstance(
        factory.create_display(" Mock ", data_dir=data_dir, settings_store=settings_store),
        MockDisplay,
    )
