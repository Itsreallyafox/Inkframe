"""HTTP API: health, status shape, settings GET/PUT, index page, logs,
restart, the fonts mount, the request guard (body caps, cross-site
requests)."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import signal
import sys
import threading
import warnings
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from PIL import Image
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from inkframe import app as app_module
from inkframe.api import system as system_api
from inkframe.app import MAX_BODY_BYTES, allowed_hosts_from_env, create_app, host_is_local
from inkframe.config import SettingsStore
from inkframe.display import factory
from inkframe.display.base import DisplayError
from inkframe.display.mock import MockDisplay, read_last_frame
from inkframe.scheduler import Scheduler
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
    # Phase 1 keys plus the scheduler's (Phase 6); nothing was refreshed yet
    assert set(s["panel"]) == {
        "state",
        "last_refresh_at",
        "last_attempt_at",
        "last_source",
        "last_error",
    }
    assert s["panel"] == {
        "state": "asleep",
        "last_refresh_at": None,
        "last_attempt_at": None,
        "last_source": None,
        "last_error": None,
    }
    assert set(s["rotation"]) == {
        "paused",
        "quiet",
        "quiet_until",
        "current",
        "next",
        "next_at",
        "keepalive_at",
        "pending",
    }
    assert s["rotation"]["paused"] is False
    assert s["rotation"]["current"] is None and s["rotation"]["pending"] is None
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
        "storage",
    }
    sysinfo = s["system"]
    assert isinstance(sysinfo["uptime_seconds"], float) and sysinfo["uptime_seconds"] >= 0
    assert isinstance(sysinfo["hostname"], str) and sysinfo["hostname"]
    assert Path(sysinfo["data_dir"]) == data_dir.resolve()
    assert isinstance(sysinfo["disk_free_bytes"], int) and sysinfo["disk_free_bytes"] > 0
    assert sysinfo["display_env"] == "mock"
    storage = sysinfo["storage"]
    assert set(storage) == {"masters_bytes", "renders_bytes", "cache_bytes", "db_bytes"}
    assert all(isinstance(v, int) and v >= 0 for v in storage.values())
    assert storage["db_bytes"] > 0  # the database exists from start-up


def test_status_storage_walks_the_data_dir(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(system_api, "STORAGE_TTL_SECONDS", 0.0)
    (data_dir / "masters" / "ab").mkdir(parents=True)
    (data_dir / "masters" / "ab" / "x.jpg").write_bytes(b"m" * 1000)
    (data_dir / "renders" / "cd").mkdir(parents=True)
    (data_dir / "renders" / "cd" / "k.png").write_bytes(b"r" * 300)
    (data_dir / "renders" / "cd" / "k.bin").write_bytes(b"r" * 200)
    (data_dir / "cache" / "weather").mkdir(parents=True, exist_ok=True)
    (data_dir / "cache" / "weather" / "f.json").write_bytes(b"c" * 70)
    storage = client.get("/api/status").json()["system"]["storage"]
    assert storage["masters_bytes"] == 1000
    assert storage["renders_bytes"] == 500
    assert storage["cache_bytes"] >= 70  # apps may keep their own small files there
    db_files = [data_dir / f"inkframe.db{s}" for s in ("", "-wal", "-shm", "-journal")]
    assert storage["db_bytes"] == sum(p.stat().st_size for p in db_files if p.exists())


def test_status_storage_is_cached_between_polls(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(system_api, "STORAGE_TTL_SECONDS", 3600.0)
    first = client.get("/api/status").json()["system"]["storage"]
    (data_dir / "masters" / "big.jpg").write_bytes(b"m" * 5000)
    assert client.get("/api/status").json()["system"]["storage"] == first
    monkeypatch.setattr(system_api, "STORAGE_TTL_SECONDS", 0.0)
    assert client.get("/api/status").json()["system"]["storage"]["masters_bytes"] == (
        first["masters_bytes"] + 5000
    )


def test_restart_answers_then_signals_itself(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    signalled = threading.Event()
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        signalled.set()

    monkeypatch.setattr(system_api, "RESTART_DELAY_SECONDS", 0.05)
    monkeypatch.setattr(system_api.os, "kill", fake_kill)
    r = client.post("/api/system/restart")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert signalled.wait(5)
    assert calls == [(os.getpid(), signal.SIGTERM)]
    assert client.get("/api/system/restart").status_code == 405


def test_fonts_are_served_with_a_long_cache(client: TestClient) -> None:
    r = client.get("/fonts/playfair-display/PlayfairDisplay-Regular.ttf")
    assert r.status_code == 200 and len(r.content) > 10_000
    assert r.headers["cache-control"] == "public, max-age=31536000"
    assert client.get("/fonts/nope.ttf").status_code == 404


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
        assert state.display is display
        # INKFRAME_SCHEDULER=off (conftest): the scheduler exists, its thread does not run
        assert isinstance(state.scheduler, Scheduler) and not state.scheduler.running
        assert set(state.apps) == {"photos", "weather", "headlines", "patterns", "quotes"}
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


# --- request guard: body caps -----------------------------------------------------------

TOO_LARGE = {"error": "request body too large (limit 1 MB)"}


def test_a_json_body_over_1_mb_is_413(client: TestClient) -> None:
    before = client.get("/api/settings").json()
    pad = b"x" * (2 * 1024 * 1024)
    body = b'{"panel": {"refresh_floor_seconds": 300}, "pad": "' + pad + b'"}'
    r = client.put("/api/settings", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json() == TOO_LARGE
    assert client.get("/api/settings").json() == before
    # the cap is on every route, whether or not it reads a body
    r = client.post("/api/display/pause", content=pad)
    assert r.status_code == 413 and r.json() == TOO_LARGE
    assert client.get("/api/status").json()["rotation"]["paused"] is False
    # a normal PUT still works
    r = client.put("/api/settings", json={"panel": {"refresh_floor_seconds": 300}})
    assert r.status_code == 200 and r.json()["panel"]["refresh_floor_seconds"] == 300


def test_the_body_cap_is_exactly_1_mib(client: TestClient) -> None:
    head = b'{"paused": true}'
    body = head + b" " * (MAX_BODY_BYTES - len(head))
    json_type = {"content-type": "application/json"}
    assert len(body) == MAX_BODY_BYTES == 1024 * 1024
    r = client.put("/api/settings", content=body, headers=json_type)
    assert r.status_code == 200 and r.json()["paused"] is True
    r = client.put("/api/settings", content=body + b" ", headers=json_type)
    assert r.status_code == 413 and r.json() == TOO_LARGE


def test_a_chunked_json_body_over_1_mb_is_413(client: TestClient) -> None:
    before = client.get("/api/settings").json()

    def chunks() -> Iterator[bytes]:
        yield b'{"paused": true, "pad": "'
        for _ in range(40):
            yield b"x" * 65536
        yield b'"}'

    # an iterator body goes out chunked, without a Content-Length
    r = client.put("/api/settings", content=chunks(), headers={"content-type": "application/json"})
    assert r.request.headers.get("transfer-encoding") == "chunked"
    assert "content-length" not in r.request.headers
    assert r.status_code == 413 and r.json() == TOO_LARGE
    assert client.get("/api/settings").json() == before
    # a small chunked body is read as usual
    r = client.put(
        "/api/settings",
        content=iter([b'{"paused": ', b"true}"]),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200 and r.json()["paused"] is True


def test_photo_uploads_are_not_capped_at_1_mb(client: TestClient) -> None:
    noise = np.random.default_rng(7).integers(0, 256, (1100, 1650, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(noise).save(buf, "JPEG", quality=95)
    data = buf.getvalue()
    assert len(data) > 1.5 * MAX_BODY_BYTES
    r = client.post("/api/photos", files={"file": ("noise.jpg", data, "image/jpeg")})
    assert r.status_code == 201, r.text
    assert r.json()["width"] == 1650


async def _read_whole_body(scope: Scope, receive: Receive, send: Send) -> None:
    """A bare ASGI app that reads its body to the end and reports its size."""
    size = 0
    while True:
        message = await receive()
        size += len(message.get("body", b""))
        if not message.get("more_body"):
            break
    await JSONResponse({"read": size})(scope, receive, send)


def _call_guard(
    method: str,
    headers: dict[str, str],
    chunks: Sequence[bytes] = (),
    *,
    path: str = "/api/settings",
    client: tuple[str, int] = ("10.0.1.20", 50123),
    allowed_hosts: frozenset[str] = frozenset(),
) -> tuple[int, dict[str, Any], int]:
    """Run the guard over ``_read_whole_body`` once; returns ``(status, JSON
    body, how many times receive() was called)``."""
    calls = 0

    async def receive() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls <= len(chunks):
            return {"type": "http.request", "body": chunks[calls - 1], "more_body": True}
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": client,
    }
    guard = app_module._RequestGuardMiddleware(_read_whole_body, allowed_hosts=allowed_hosts)
    asyncio.run(guard(scope, receive, send))
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], json.loads(body), calls


def test_a_chunked_body_is_cut_off_at_the_cap_not_read_to_the_end() -> None:
    lan = {"host": "inkframe.local"}
    status, body, calls = _call_guard("PUT", lan, [b"x" * 65536] * 64)  # 4 MiB
    assert status == 413 and body == TOO_LARGE
    assert calls == MAX_BODY_BYTES // 65536 + 1  # stopped on the first chunk past 1 MiB
    status, body, _ = _call_guard("PUT", lan, [b"x" * 65536] * 16)  # exactly 1 MiB
    assert status == 200 and body == {"read": MAX_BODY_BYTES}


def test_an_oversized_content_length_is_refused_before_the_body_is_read() -> None:
    lan = {"host": "inkframe.local"}
    status, body, calls = _call_guard("PUT", {**lan, "content-length": str(2 * 1024 * 1024)})
    assert status == 413 and body == TOO_LARGE and calls == 0
    status, body, calls = _call_guard("PUT", {**lan, "content-length": "lots"})
    assert status == 400 and body == {"error": "invalid Content-Length"} and calls == 0
    # the upload route keeps its own (larger) limit
    upload = {**lan, "content-length": str(2 * 1024 * 1024)}
    status, body, _ = _call_guard("POST", upload, [b"x" * 65536] * 32, path="/api/photos")
    assert status == 200 and body == {"read": 2 * 1024 * 1024}


# --- request guard: cross-site requests -------------------------------------------------

UNEXPECTED_HOST = {"error": "request refused: unexpected Host"}
CROSS_SITE = {"error": "request refused: cross-site origin"}


@pytest.fixture
def lan_client(
    data_dir: Path, mock_display: MockDisplay, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """``client`` with no ``INKFRAME_ALLOWED_HOSTS`` from the environment."""
    monkeypatch.delenv("INKFRAME_ALLOWED_HOSTS", raising=False)
    app = create_app(data_dir=data_dir, display=mock_display, scheduler=False)
    with TestClient(app) as c:
        yield c


def _paused(c: TestClient) -> bool:
    return c.get("/api/status").json()["rotation"]["paused"]


def test_a_cross_site_post_is_refused(lan_client: TestClient) -> None:
    r = lan_client.post("/api/display/pause", headers={"origin": "http://evil.example"})
    assert r.status_code == 403 and r.json() == CROSS_SITE
    assert not _paused(lan_client)
    # Origin "null" (sandboxed frames, file: pages) is cross-site too
    r = lan_client.post("/api/display/pause", headers={"origin": "null"})
    assert r.status_code == 403 and r.json() == CROSS_SITE
    # browsers that send Sec-Fetch-Site: cross-site, with or without Origin
    r = lan_client.post("/api/display/pause", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403 and r.json() == CROSS_SITE
    # a multipart upload (a "simple" request no preflight stops) as well
    r = lan_client.post(
        "/api/photos",
        files={"file": ("a.jpg", b"\xff\xd8\xff", "image/jpeg")},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403 and r.json() == CROSS_SITE
    assert lan_client.get("/api/photos").json()["count"] == 0
    r = lan_client.put(
        "/api/settings", json={"paused": True}, headers={"origin": "http://evil.example"}
    )
    assert r.status_code == 403 and not _paused(lan_client)


def test_a_same_origin_post_is_accepted(lan_client: TestClient) -> None:
    r = lan_client.post(
        "/api/display/pause",
        headers={"origin": "http://testserver", "sec-fetch-site": "same-origin"},
    )
    assert r.status_code == 200 and _paused(lan_client)
    r = lan_client.post(
        "/api/display/resume",
        headers={"host": "Inkframe.local:8080", "origin": "http://inkframe.local:8080"},
    )
    assert r.status_code == 200 and not _paused(lan_client)
    # a request typed or bookmarked by the owner: Sec-Fetch-Site none
    r = lan_client.post("/api/display/pause", headers={"sec-fetch-site": "none"})
    assert r.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "inkframe.local",
        "inkframe.local:8080",
        "INKFRAME.LOCAL.",
        "10.0.1.238",
        "10.0.1.238:80",
        "192.168.1.20",
        "172.16.4.4",
        "localhost",
        "localhost:8080",
        "127.0.0.1",
        "[::1]:8080",
        "[fd12:3456::7]",
        "169.254.10.1",
        "[fe80::1]",
        "inkframe",
    ],
)
def test_lan_hosts_are_accepted(lan_client: TestClient, host: str) -> None:
    r = lan_client.post("/api/display/pause", headers={"host": host})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:80",
        "8.8.8.8",
        "inkframe.local.evil.example",
        "localhost.evil.example",
        "evil.example@localhost",
        "localhost:http",
        "[::1",
    ],
)
def test_other_hosts_are_refused(lan_client: TestClient, host: str) -> None:
    # DNS rebinding: Origin and Host agree, but name a site that is not the frame
    r = lan_client.post("/api/display/pause", headers={"host": host, "origin": f"http://{host}"})
    assert r.status_code == 403 and r.json() == UNEXPECTED_HOST
    assert not _paused(lan_client)


def test_origin_must_name_the_host(lan_client: TestClient) -> None:
    for origin in (
        "http://10.0.1.238",
        "http://inkframe.local.evil.example",
        "ftp://inkframe.local",
    ):
        r = lan_client.post(
            "/api/display/pause", headers={"host": "inkframe.local", "origin": origin}
        )
        assert r.status_code == 403 and r.json() == CROSS_SITE, origin
    assert not _paused(lan_client)


def test_get_is_never_blocked(lan_client: TestClient) -> None:
    hostile = {
        "host": "evil.example",
        "origin": "http://evil.example",
        "sec-fetch-site": "cross-site",
    }
    assert lan_client.get("/api/status", headers=hostile).status_code == 200
    assert lan_client.head("/api/health", headers=hostile).status_code == 200
    assert lan_client.get("/", headers=hostile).status_code == 200
    # OPTIONS is not checked either (there is no CORS: nothing answers it)
    assert lan_client.options("/api/display/pause", headers=hostile).status_code == 405


def test_allowed_hosts_env_var_adds_names(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INKFRAME_ALLOWED_HOSTS", " frame.example.com, Photo.Example.org:8080,,")
    app = create_app(data_dir=data_dir, display=MockDisplay(data_dir), scheduler=False)
    # read once, by create_app
    monkeypatch.setenv("INKFRAME_ALLOWED_HOSTS", "other.example.com")
    with TestClient(app) as c:
        for host in ("frame.example.com", "frame.example.com:443", "photo.example.org"):
            assert c.post("/api/display/pause", headers={"host": host}).status_code == 200
        r = c.post("/api/display/pause", headers={"host": "other.example.com"})
        assert r.status_code == 403 and r.json() == UNEXPECTED_HOST


def test_allowed_hosts_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.socket, "gethostname", lambda: "Kitchen-Frame")
    monkeypatch.setenv("INKFRAME_ALLOWED_HOSTS", "2001:db8::1, [2001:DB8:0::2]:80, a.b.")
    assert allowed_hosts_from_env() == {"kitchen-frame", "2001:db8::1", "2001:db8::2", "a.b"}
    monkeypatch.delenv("INKFRAME_ALLOWED_HOSTS")
    assert allowed_hosts_from_env() == {"kitchen-frame"}


def test_the_machine_hostname_is_accepted(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INKFRAME_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(app_module.socket, "gethostname", lambda: "Kitchen-Frame")
    app = create_app(data_dir=data_dir, display=MockDisplay(data_dir), scheduler=False)
    with TestClient(app) as c:
        assert c.post("/api/display/pause", headers={"host": "kitchen-frame"}).status_code == 200


@pytest.mark.parametrize(
    ("name", "local"),
    [
        ("localhost", True),
        ("inkframe", True),
        ("frame.local", True),
        ("10.1.2.3", True),
        ("::1", True),
        ("fe80::1", True),
        ("::ffff:192.168.0.9", True),
        ("::ffff:8.8.8.8", False),
        ("8.8.8.8", False),
        ("2001:4860::8888", False),
        ("testserver", False),
        ("local", False),
        ("example.com", False),
    ],
)
def test_host_is_local(name: str, local: bool) -> None:
    assert host_is_local(name) is local
    assert host_is_local(name, frozenset({name})) is True


def test_the_test_client_exemption_is_only_for_the_test_client() -> None:
    # Host "testserver" is local only for Starlette's TestClient
    status, body, _ = _call_guard("POST", {"host": "testserver"}, client=("10.0.1.20", 50123))
    assert status == 403 and body == UNEXPECTED_HOST
    status, body, _ = _call_guard("POST", {"host": "testserver"}, client=("testclient", 50000))
    assert status == 200
    # and never without a Host header at all
    status, body, _ = _call_guard("POST", {}, client=("testclient", 50000))
    assert status == 403 and body == UNEXPECTED_HOST
