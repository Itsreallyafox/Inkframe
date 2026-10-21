"""HTTP: /api/apps (list, per-app settings, preview.png).

Previews run on the render executor with ``INKFRAME_OFFLINE=1`` (conftest),
so every app renders deterministically without the network: weather with no
location draws its "where are you?" card, headlines its offline card, quotes
the bundled library, patterns needs no network, photos the stub's cards.
"""

from __future__ import annotations

import io
import threading
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from inkframe.apps import weather
from inkframe.apps.base import RenderContext
from inkframe.display.mock import MockDisplay
from inkframe.display.palette import MEASURED_DEFAULT
from inkframe.scheduler import local_now
from tests.conftest import gradient_image, jpeg_bytes

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
APP_IDS = ["photos", "weather", "headlines", "patterns", "quotes"]


def _png(r) -> Image.Image:
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-store"
    assert r.content[:8] == PNG_MAGIC
    img = Image.open(io.BytesIO(r.content))
    img.load()
    return img


def _colours(img: Image.Image) -> set[tuple[int, int, int]]:
    pixels = np.asarray(img.convert("RGB")).reshape(-1, 3)
    return {tuple(int(v) for v in c) for c in np.unique(pixels, axis=0)}


# --- list and settings ---------------------------------------------------------------


def test_list_apps(client: TestClient) -> None:
    r = client.get("/api/apps")
    assert r.status_code == 200
    apps = r.json()
    assert [a["id"] for a in apps] == APP_IDS
    for a in apps:
        assert set(a) == {
            "id",
            "name",
            "description",
            "needs_dither",
            "variants",
            "settings_schema",
            "settings",
        }
        assert isinstance(a["settings_schema"], list) and isinstance(a["settings"], dict)
        assert {f["key"] for f in a["settings_schema"]} == set(a["settings"])
    by_id = {a["id"]: a for a in apps}
    assert by_id["photos"]["settings_schema"] == [] and by_id["photos"]["variants"] == 1
    assert by_id["weather"]["settings"]["units"] == "metric"
    loc = next(f for f in by_id["weather"]["settings_schema"] if f["key"] == "location")
    assert loc["required"] is True and loc["type"] == "text"


def test_app_settings_get_put(client: TestClient) -> None:
    r = client.get("/api/apps/headlines/settings")
    assert r.status_code == 200
    defaults = r.json()
    assert defaults["count"] == 5

    r = client.put("/api/apps/headlines/settings", json={"count": 6, "mix": "by_feed"})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved == {**defaults, "count": 6, "mix": "by_feed"}
    assert client.get("/api/apps/headlines/settings").json() == saved
    # a partial update merges over what was saved
    r = client.put("/api/apps/headlines/settings", json={"max_age_hours": 12})
    assert r.json() == {**saved, "max_age_hours": 12}
    # only the keys the user set are stored; the rest keep following the defaults
    stored = client.app.state.db.get_app_settings("headlines")
    assert stored == {"count": 6, "mix": "by_feed", "max_age_hours": 12}
    assert {a["id"]: a for a in client.get("/api/apps").json()}["headlines"]["settings"] == r.json()


def test_weather_location_stays_required(client: TestClient) -> None:
    # the default (empty) location is fine until the user sets something else ...
    r = client.put("/api/apps/weather/settings", json={"units": "imperial"})
    assert r.status_code == 200, r.text
    assert r.json()["units"] == "imperial" and r.json()["location"] == ""
    # ... but saving it empty is refused
    r = client.put("/api/apps/weather/settings", json={"location": "  "})
    assert r.status_code == 422 and "location" in r.json()["error"]
    r = client.put("/api/apps/weather/settings", json={"location": "Berlin"})
    assert r.status_code == 200 and r.json()["location"] == "Berlin"


@pytest.mark.parametrize(
    ("app_id", "body", "fragment"),
    [
        ("headlines", {"count": 99}, "count"),
        ("headlines", {"count": "many"}, "count"),
        ("headlines", {"colour": "red"}, "unknown setting"),
        ("weather", {"units": "kelvin"}, "units"),
        ("photos", {"dwell_minutes": 3}, "unknown setting"),
    ],
)
def test_app_settings_put_validation(
    client: TestClient, app_id: str, body: dict[str, Any], fragment: str
) -> None:
    before = client.get(f"/api/apps/{app_id}/settings").json()
    r = client.put(f"/api/apps/{app_id}/settings", json=body)
    assert r.status_code == 422
    assert fragment in r.json()["error"]
    assert client.get(f"/api/apps/{app_id}/settings").json() == before


def test_app_settings_unknown_app_and_bad_body(client: TestClient) -> None:
    assert client.get("/api/apps/radio/settings").status_code == 404
    r = client.put("/api/apps/radio/settings", json={})
    assert r.status_code == 404 and "unknown app" in r.json()["error"]
    assert client.put("/api/apps/quotes/settings", json=[1, 2]).status_code == 422


def test_stale_stored_settings_fall_back_to_defaults(client: TestClient) -> None:
    client.app.state.db.set_app_settings(
        "headlines", {"count": 99, "gone_key": 1, "mix": "by_feed"}
    )
    s = client.get("/api/apps/headlines/settings").json()
    assert s["count"] == 5 and s["mix"] == "by_feed" and "gone_key" not in s
    r = client.put("/api/apps/headlines/settings", json={"count": 4})
    assert r.status_code == 200 and r.json()["count"] == 4
    assert "gone_key" not in client.app.state.db.get_app_settings("headlines")


# --- previews ---------------------------------------------------------------------------


@pytest.mark.parametrize("app_id", APP_IDS)
def test_preview_png_for_every_app(
    client: TestClient, mock_display: MockDisplay, app_id: str
) -> None:
    img = _png(client.get(f"/api/apps/{app_id}/preview.png", params={"seed": 7}))
    assert img.size == (600, 400) and img.mode == "RGB"
    assert _colours(img) <= set(MEASURED_DEFAULT.rgb)  # what the panel would show
    assert len(_colours(img)) >= 2
    # a preview never touches the panel
    assert mock_display.show_count == 0 and mock_display.awake is False
    assert mock_display.last_show_at is None


def test_preview_variants_and_seed(client: TestClient) -> None:
    card = _png(client.get("/api/apps/patterns/preview.png", params={"variant": 2}))
    again = _png(client.get("/api/apps/patterns/preview.png", params={"variant": 2}))
    assert card.tobytes() == again.tobytes()  # the test card is deterministic
    a = _png(client.get("/api/apps/patterns/preview.png", params={"variant": 0, "seed": 1}))
    b = _png(client.get("/api/apps/patterns/preview.png", params={"variant": 0, "seed": 1}))
    c = _png(client.get("/api/apps/patterns/preview.png", params={"variant": 0, "seed": 2}))
    assert a.tobytes() == b.tobytes() != c.tobytes()


def test_preview_uses_the_measured_palette_setting(client: TestClient) -> None:
    client.put("/api/settings", json={"palette": {"measured": {"white": [250, 249, 240]}}})
    img = _png(client.get("/api/apps/photos/preview.png"))
    assert (250, 249, 240) in _colours(img)


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"variant": 3}, 422),  # patterns has 3 variants: 0..2
        ({"variant": -1}, 422),
        ({"seed": -5}, 422),
        ({"seed": "x"}, 422),
    ],
)
def test_preview_rejects_bad_params(client: TestClient, params: dict, status: int) -> None:
    r = client.get("/api/apps/patterns/preview.png", params=params)
    assert r.status_code == status and "error" in r.json()


def test_preview_unknown_app_is_404(client: TestClient) -> None:
    r = client.get("/api/apps/radio/preview.png")
    assert r.status_code == 404 and r.json() == {"error": "unknown app 'radio'"}


def test_preview_app_failure_is_500(client: TestClient) -> None:
    class Broken:
        id = "broken"
        name = "Broken"
        description = "always fails"
        needs_dither = False
        settings_schema = ()
        variants = 1

        def render(self, ctx: RenderContext):
            raise RuntimeError("no ink left")

    client.app.state.apps = {**client.app.state.apps, "broken": Broken()}
    r = client.get("/api/apps/broken/preview.png")
    assert r.status_code == 500
    assert "no ink left" in r.json()["error"]


def test_preview_runs_on_the_render_thread(client: TestClient) -> None:
    seen: list[str] = []

    class Spy:
        id = "spy"
        name = "Spy"
        description = "records its thread"
        needs_dither = False
        settings_schema = ()
        variants = 1

        def render(self, ctx: RenderContext):
            seen.append(threading.current_thread().name)
            return np.ones((400, 600), dtype=np.uint8)

    client.app.state.apps = {**client.app.state.apps, "spy": Spy()}
    _png(client.get("/api/apps/spy/preview.png"))
    assert len(seen) == 1 and seen[0].startswith("render")


def test_photos_preview_uses_the_current_photo(client: TestClient) -> None:
    db = client.app.state.db
    empty = _png(client.get("/api/apps/photos/preview.png"))  # the "no photos" card
    for name, image in (
        ("first.jpg", gradient_image(600, 400)),
        ("second.jpg", gradient_image(600, 400).transpose(Image.Transpose.FLIP_LEFT_RIGHT)),
    ):
        data = jpeg_bytes(image)
        r = client.post("/api/photos", files={"file": (name, data, "image/jpeg")})
        assert r.status_code == 201, r.text
    second = db.list_photos()[1]
    one = _png(client.get("/api/apps/photos/preview.png"))  # nothing current: the first photo
    assert one.tobytes() != empty.tobytes()
    client.post("/api/display/show", json={"photo_id": second.id})
    sched = client.app.state.scheduler
    sched.tick(local_now())
    assert sched.current_photo_id() == second.id
    two = _png(client.get("/api/apps/photos/preview.png"))
    assert two.tobytes() not in (one.tobytes(), empty.tobytes())


def test_offline_env_var_reaches_the_apps(client: TestClient, monkeypatch) -> None:
    calls: list[str] = []

    def no_network(url: str, params: dict, timeout: float) -> Any:
        calls.append(url)
        raise weather.FetchError("test: no network")

    monkeypatch.setattr(weather, "fetch_json", no_network)
    monkeypatch.setattr(weather, "_sleep", lambda seconds: None)  # no real pause before a retry
    client.put("/api/apps/weather/settings", json={"location": "Berlin"})
    _png(client.get("/api/apps/weather/preview.png"))
    assert calls == []  # INKFRAME_OFFLINE=1: the app was told not to try

    monkeypatch.delenv("INKFRAME_OFFLINE")
    _png(client.get("/api/apps/weather/preview.png"))  # online: it tries, fails, draws a card
    assert calls and "geocoding" in calls[0]
