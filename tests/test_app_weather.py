"""The weather app: data flow, cache, fallbacks, every variant and every card.

All tests are offline: the fixtures in ``tests/fixtures/weather`` are real
Open-Meteo responses (Berlin metric, Austin TX imperial) recorded on
2026-10-19 at 19:13 UTC, and ``weather.fetch_json`` is replaced whenever a
test wants the "network" path.  Real HTTP is blocked by an autouse fixture.
"""

from __future__ import annotations

import copy
import json
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from inkframe.apps import load_app, weather, weather_layouts
from inkframe.apps.base import (
    App,
    FontLibrary,
    RenderContext,
    render_to_frame,
    schema_defaults,
    validate_settings,
)
from inkframe.apps.draw import Canvas
from inkframe.apps.weather_glyphs import KINDS, draw_glyph, glyph_for
from inkframe.apps.weather_layouts import M, Style, W, meta_line
from inkframe.apps.weather_model import (
    Place,
    band_for,
    condition_text,
    fmt_clock,
    fmt_coords,
    fmt_temp,
    parse_forecast,
    round_half_up,
)
from inkframe.display.palette import BLACK, BLUE, RED, WHITE, YELLOW

FIX = Path(__file__).parent / "fixtures" / "weather"
FONTS = FontLibrary()  # shared: fonts are cached per size, which keeps the suite quick

BERLIN_TZ = timezone(timedelta(hours=2))
AUSTIN_TZ = timezone(timedelta(hours=-5))
#: 21:15 in Berlin, 14:15 in Austin: two minutes after the fixtures were recorded
NOW = datetime(2026, 10, 19, 19, 15, tzinfo=timezone.utc)
BERLIN_NOW = NOW.astimezone(BERLIN_TZ)
AUSTIN_NOW = NOW.astimezone(AUSTIN_TZ)

ALL_WMO = (0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77)
ALL_WMO += (80, 81, 82, 85, 86, 95, 96, 99)


def fixture(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


CITIES = {
    "berlin": {
        "location": "Berlin",
        "units": "metric",
        "now": BERLIN_NOW,
        "geocode": "geocode_berlin.json",
        "forecast": "forecast_berlin.json",
    },
    "austin": {
        "location": "Austin, TX",
        "units": "imperial",
        "now": AUSTIN_NOW,
        "geocode": "geocode_austin_tx.json",
        "forecast": "forecast_austin_tx.json",
    },
}


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("a test tried to use the real network")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replaces the pause before a retry: no test sleeps, and a test that asks for
    this fixture sees the delays the app wanted."""
    delays: list[float] = []
    monkeypatch.setattr(weather, "_sleep", delays.append)
    return delays


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> list[float]:
    """A frozen monotonic clock for the app, ``clock[0]`` seconds.  Only the retry
    pause moves it on (by the delay it asked for, still recorded in ``sleeps``), and
    a test may set it to stand for time a fake fetch used, so budgets are exact."""
    now = [1000.0]
    monkeypatch.setattr(weather, "time", types.SimpleNamespace(monotonic=lambda: now[0]))

    def pause(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(weather, "_sleep", pause)
    return now


def overloaded() -> weather.FetchError:
    """What ``fetch_json`` raises for Open-Meteo's 503 (transient)."""
    return weather.FetchError("HTTP 503: The service is overloaded", transient=True)


class FakeNet:
    """Stands in for ``weather.fetch_json``: serves fixtures, records calls.

    ``fail=True`` fails every call for good (a non-transient error, never retried);
    ``fail=<FetchError>`` raises that on every call; ``errors`` are raised by the
    first calls, in order, before normal service."""

    def __init__(
        self,
        forecast: Any = None,
        geocode: dict[str, Any] | None = None,
        fail: bool | weather.FetchError = False,
        errors: tuple[weather.FetchError, ...] = (),
    ) -> None:
        self.forecast = forecast
        self.geocode = geocode or {}
        self.fail = fail
        self.errors = list(errors)
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def __call__(self, url: str, params: dict[str, Any], timeout: float) -> Any:
        self.calls.append((url, dict(params), timeout))
        if self.errors:
            raise self.errors.pop(0)
        if isinstance(self.fail, weather.FetchError):
            raise self.fail
        if self.fail:
            raise weather.FetchError("HTTP 404: Not Found", transient=False)
        if url == weather.GEOCODE_URL:
            return self.geocode.get(params["name"], {"generationtime_ms": 0.1})
        assert url == weather.FORECAST_URL
        return copy.deepcopy(self.forecast)

    def urls(self) -> list[str]:
        return [u for u, _, _ in self.calls]


def city_net(city: str, errors: tuple[weather.FetchError, ...] = ()) -> FakeNet:
    c = CITIES[city]
    return FakeNet(
        forecast=fixture(c["forecast"]),
        geocode={c["location"]: fixture(c["geocode"])},
        errors=errors,
    )


def make_ctx(
    cache: Path,
    city: str | None = "berlin",
    *,
    online: bool = False,
    now: datetime | None = None,
    variant: int | None = None,
    seed: int | None = None,
    **settings: Any,
) -> RenderContext:
    data: dict[str, Any] = {}
    if city is not None:
        data = {"location": CITIES[city]["location"], "units": CITIES[city]["units"]}
    data.update(settings)
    if data.get("location"):
        s = validate_settings(weather.SETTINGS_SCHEMA, data)
    else:
        s = {**schema_defaults(weather.SETTINGS_SCHEMA), **data}
    return RenderContext(
        settings=s,
        now=now or (CITIES[city]["now"] if city else BERLIN_NOW),
        cache_dir=cache,
        fonts=FONTS,
        online=online,
        variant=variant,
        seed=seed,
    )


def seed_cache(cache: Path, city: str, fetched_at: datetime, data: Any = None) -> Place:
    """Put a city into the cache exactly as the app would have written it."""
    c = CITIES[city]
    place = weather.place_from_result(fixture(c["geocode"])["results"][0])
    entry = place.to_dict()
    (cache / weather.GEOCODE_FILE).write_text(
        json.dumps({c["location"].casefold(): entry}), encoding="utf-8"
    )
    key = weather.forecast_key(place.latitude, place.longitude, c["units"])
    weather.write_forecast_cache(cache, key, data or fixture(c["forecast"]), fetched_at)
    return place


def outcome(ctx: RenderContext) -> weather.Outcome:
    return weather.load_weather(ctx, ctx.settings)


def frame_of(ctx: RenderContext) -> np.ndarray:
    frame = render_to_frame(weather.APP, ctx)
    assert frame.shape == (400, 600) and frame.dtype == np.uint8
    assert set(np.unique(frame).tolist()) <= set(range(6))
    return frame


# --- contract and settings -------------------------------------------------------------


def test_app_satisfies_the_contract() -> None:
    assert isinstance(weather.APP, App)
    assert weather.APP.id == "weather" == weather.__name__.rsplit(".", 1)[1]
    assert weather.APP.name == "Weather"
    assert weather.APP.needs_dither is False
    assert weather.APP.variants == 3
    assert load_app("weather") is weather.APP


def test_settings_schema_defaults_and_validation() -> None:
    schema = weather.SETTINGS_SCHEMA
    assert 3 <= len(schema) <= 4
    defaults = schema_defaults(schema)
    assert defaults == {"location": "", "units": "metric", "layout": "rotate", "label": ""}
    loc = next(f for f in schema if f.key == "location")
    assert loc.required and loc.help == "Place name, or latitude,longitude"
    ok = validate_settings(schema, {"location": " Berlin ", "units": "imperial"})
    assert ok["location"] == "Berlin" and ok["units"] == "imperial"
    with pytest.raises(ValueError, match="location: required"):
        validate_settings(schema, {"location": "  "})
    with pytest.raises(ValueError, match="units"):
        validate_settings(schema, {"location": "Berlin", "units": "kelvin"})
    with pytest.raises(ValueError, match="layout"):
        validate_settings(schema, {"location": "Berlin", "layout": "giant"})


def test_parse_coords() -> None:
    assert weather.parse_coords("52.52,13.41") == (52.52, 13.41)
    assert weather.parse_coords(" -33.87 , 151.21 ") == (-33.87, 151.21)
    assert weather.parse_coords("40.7 -74.0") == (40.7, -74.0)
    assert weather.parse_coords("Berlin") is None
    assert weather.parse_coords("10115") is None  # a postcode is a place name
    with pytest.raises(ValueError):
        weather.parse_coords("95, 200")


def test_place_names_read_naturally() -> None:
    berlin = weather.place_from_result(fixture("geocode_berlin.json")["results"][0])
    austin = weather.place_from_result(fixture("geocode_austin_tx.json")["results"][0])
    assert berlin.display == "Berlin, Germany"
    assert austin.display == "Austin, Texas"
    assert (round(berlin.latitude, 2), round(berlin.longitude, 2)) == (52.52, 13.41)
    assert Place.from_dict(austin.to_dict()) == austin
    assert fmt_coords(52.52, -13.4) == "52.52°N 13.40°W"


# --- network and cache --------------------------------------------------------------------


def test_fetch_then_cache_then_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    net = city_net("berlin")
    monkeypatch.setattr(weather, "fetch_json", net)

    frame_of(make_ctx(tmp_path, online=True, variant=0))
    assert net.urls() == [weather.GEOCODE_URL, weather.FORECAST_URL]
    _, geo_params, timeout = net.calls[0]
    assert geo_params == {"name": "Berlin", "count": 1, "language": "en", "format": "json"}
    assert 0 < timeout <= 10
    _, fc, _ = net.calls[1]
    assert fc["latitude"] == "52.5244" and fc["longitude"] == "13.4105"
    assert fc["timezone"] == "auto" and fc["forecast_days"] == 5
    assert fc["current"].split(",") == [
        "temperature_2m",
        "apparent_temperature",
        "relative_humidity_2m",
        "weather_code",
        "wind_speed_10m",
        "is_day",
    ]
    assert "precipitation_probability" in fc["hourly"]
    assert "sunrise" in fc["daily"] and "precipitation_probability_max" in fc["daily"]
    assert "temperature_unit" not in fc and "wind_speed_unit" not in fc

    geo = json.loads((tmp_path / "geocode.json").read_text(encoding="utf-8"))
    assert geo["berlin"]["name"] == "Berlin" and geo["berlin"]["region"] == "Germany"
    fc_cache = json.loads((tmp_path / "forecast.json").read_text(encoding="utf-8"))
    entry = fc_cache["52.5244,13.4105,metric"]
    assert datetime.fromisoformat(entry["fetched_at"]) == NOW
    assert entry["data"]["current"]["temperature_2m"] == 8.1

    # inside the 15-minute window nothing is fetched, not even with the network on
    frame_of(make_ctx(tmp_path, online=True, variant=1, now=BERLIN_NOW + timedelta(minutes=14)))
    assert len(net.calls) == 2
    # after it, only the forecast is refreshed: the place is cached forever
    later = BERLIN_NOW + timedelta(minutes=16)
    o = outcome(make_ctx(tmp_path, online=True, now=later))
    assert net.urls()[2:] == [weather.FORECAST_URL]
    assert o.state == "ok" and not o.weather.offline


def test_imperial_asks_for_fahrenheit_and_mph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    net = city_net("austin")
    monkeypatch.setattr(weather, "fetch_json", net)
    o = outcome(make_ctx(tmp_path, "austin", online=True))
    assert o.state == "ok" and o.weather.place.display == "Austin, Texas"
    fc = net.calls[1][1]
    assert fc["temperature_unit"] == "fahrenheit" and fc["wind_speed_unit"] == "mph"
    assert "30.2672,-97.7431,imperial" in json.loads(
        (tmp_path / "forecast.json").read_text(encoding="utf-8")
    )


def test_geocoding_retries_without_the_qualifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    net = FakeNet(
        forecast=fixture("forecast_austin_tx.json"),
        geocode={"Austin": fixture("geocode_austin_tx.json")},
    )
    monkeypatch.setattr(weather, "fetch_json", net)
    o = outcome(make_ctx(tmp_path, "austin", online=True, location="Austin, Nowhere"))
    assert o.state == "ok"
    assert [p["name"] for u, p, _ in net.calls if u == weather.GEOCODE_URL] == [
        "Austin, Nowhere",
        "Austin",
    ]


def test_unknown_place_gets_its_own_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    net = FakeNet(forecast=fixture("forecast_berlin.json"))
    monkeypatch.setattr(weather, "fetch_json", net)
    ctx = make_ctx(tmp_path, online=True, location="Atlantis")
    assert outcome(ctx).state == "not_found"
    frame = frame_of(ctx)
    assert (frame == RED).sum() > 500  # the red pin
    assert weather.FORECAST_URL not in net.urls()
    assert not (tmp_path / "geocode.json").exists()  # a miss is not cached


def test_coordinates_skip_geocoding_and_label_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    net = FakeNet(forecast=fixture("forecast_berlin.json"))
    monkeypatch.setattr(weather, "fetch_json", net)
    o = outcome(make_ctx(tmp_path, online=True, location="52.52, 13.41"))
    assert net.urls() == [weather.FORECAST_URL]
    assert o.weather.place.display == "52.52°N 13.41°E"
    o = outcome(make_ctx(tmp_path, online=False, location="52.52,13.41", label="Home"))
    assert o.weather.place.display == "Home"
    for v in range(3):  # a name made of figures must still render (no Playfair numerals)
        frame_of(make_ctx(tmp_path, location="52.52,13.41", variant=v))

    # the label also replaces a geocoded name
    seed_cache(tmp_path, "berlin", NOW)
    o = outcome(make_ctx(tmp_path, label="Studio"))
    assert o.weather.place.display == "Studio"


def test_stale_cache_is_shown_when_the_refresh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_cache(tmp_path, "berlin", NOW - timedelta(hours=3))
    net = FakeNet(fail=True)
    monkeypatch.setattr(weather, "fetch_json", net)
    ctx = make_ctx(tmp_path, online=True, variant=0)
    o = outcome(ctx)
    assert net.urls() == [weather.FORECAST_URL]  # it did try
    assert o.state == "ok" and o.weather.offline
    text, fill = meta_line(o.weather, ctx.now, Style(FONTS))
    assert (text, fill) == ("COULDN'T UPDATE, DATA FROM 18:15", RED)  # Berlin time
    for v in range(3):
        frame = frame_of(make_ctx(tmp_path, online=True, variant=v))
        assert (frame == RED).sum() > 100  # the red offline line


def test_a_transient_failure_is_retried_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float], clock: list[float]
) -> None:
    seed_cache(tmp_path, "berlin", NOW - timedelta(hours=3))
    net = city_net("berlin", errors=(overloaded(),))
    monkeypatch.setattr(weather, "fetch_json", net)
    ctx = make_ctx(tmp_path, online=True)
    assert ctx.http_timeout == 10.0
    o = outcome(ctx)
    assert net.urls() == [weather.FORECAST_URL, weather.FORECAST_URL]
    assert sleeps == [weather.RETRY_DELAY]
    # the first attempt gets the whole budget, the retry what is left after the pause
    assert net.calls[0][2] == 10.0
    assert net.calls[1][2] == pytest.approx(10.0 - weather.RETRY_DELAY)
    assert o.state == "ok" and not o.weather.offline and o.detail == ""
    entry = json.loads((tmp_path / "forecast.json").read_text(encoding="utf-8"))
    assert datetime.fromisoformat(entry["52.5244,13.4105,metric"]["fetched_at"]) == NOW


def test_a_permanent_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    seed_cache(tmp_path, "berlin", NOW - timedelta(hours=3))
    net = FakeNet(fail=weather.FetchError("HTTP 400: Invalid parameter", transient=False))
    monkeypatch.setattr(weather, "fetch_json", net)
    o = outcome(make_ctx(tmp_path, online=True))
    assert net.urls() == [weather.FORECAST_URL] and sleeps == []
    assert o.state == "ok" and o.weather.offline and o.detail == "HTTP 400: Invalid parameter"


def test_no_retry_when_the_budget_has_no_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    seed_cache(tmp_path, "berlin", NOW - timedelta(hours=3))
    net = FakeNet(fail=overloaded())
    monkeypatch.setattr(weather, "fetch_json", net)
    ctx = make_ctx(tmp_path, online=True)
    ctx.http_timeout = weather.RETRY_DELAY + 0.5  # less than the delay plus a second left
    o = outcome(ctx)
    assert net.urls() == [weather.FORECAST_URL] and sleeps == []
    assert o.state == "ok" and o.weather.offline


@pytest.mark.parametrize(("left", "retried"), [(3.0, False), (3.05, True)])
def test_a_retry_needs_more_than_the_delay_plus_a_second(
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
    clock: list[float],
    left: float,
    retried: bool,
) -> None:
    """The first attempt uses all but ``left`` seconds of the budget: the retry
    happens only if more than ``RETRY_DELAY + 1`` s are left."""
    assert weather.RETRY_DELAY + 1.0 == 3.0
    deadline = clock[0] + 10.0
    calls: list[float] = []

    def slow_then_ok(url: str, params: dict[str, Any], timeout: float) -> Any:
        calls.append(timeout)
        if len(calls) == 1:
            clock[0] = deadline - left
            raise overloaded()
        return {"ok": True}

    monkeypatch.setattr(weather, "fetch_json", slow_then_ok)
    ctx = RenderContext(settings={}, now=NOW, cache_dir=Path("."), fonts=FONTS)
    if retried:
        assert weather._fetch(weather.FORECAST_URL, {}, ctx, deadline) == {"ok": True}
        assert calls == [10.0, pytest.approx(left - weather.RETRY_DELAY)]
        assert sleeps == [weather.RETRY_DELAY]
    else:
        with pytest.raises(weather.FetchError, match="HTTP 503"):
            weather._fetch(weather.FORECAST_URL, {}, ctx, deadline)
        assert calls == [10.0] and sleeps == []


def test_a_failed_retry_shows_the_cache_as_couldnt_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    seed_cache(tmp_path, "berlin", NOW - timedelta(hours=3))
    net = FakeNet(fail=overloaded())
    monkeypatch.setattr(weather, "fetch_json", net)
    ctx = make_ctx(tmp_path, online=True, variant=0)
    o = outcome(ctx)
    assert net.urls() == [weather.FORECAST_URL] * 2  # one retry, never more
    assert sleeps == [weather.RETRY_DELAY]
    assert o.state == "ok" and o.weather.offline and o.weather.frame_online
    assert o.detail == "HTTP 503: The service is overloaded"
    text, fill = meta_line(o.weather, ctx.now, Style(FONTS))
    assert (text, fill) == ("COULDN'T UPDATE, DATA FROM 18:15", RED)  # Berlin time

    # every layout, through the app, draws exactly that stamp in red
    canvases: list[TextLog] = []

    def logging_canvas(background: int = WHITE) -> TextLog:
        canvases.append(TextLog(background))
        return canvases[-1]

    monkeypatch.setattr(weather, "Canvas", logging_canvas)
    for v in range(3):
        frame_of(make_ctx(tmp_path, online=True, variant=v))
        drawn = [(t, f) for t, f, _ in canvases[-1].texts]
        assert (text, RED) in drawn, (v, drawn)


def test_geocoding_is_retried_once_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    net = city_net("berlin", errors=(weather.FetchError("ConnectTimeout: timed out"),))
    monkeypatch.setattr(weather, "fetch_json", net)
    o = outcome(make_ctx(tmp_path, online=True))
    assert net.urls() == [weather.GEOCODE_URL, weather.GEOCODE_URL, weather.FORECAST_URL]
    assert sleeps == [weather.RETRY_DELAY]
    assert o.state == "ok" and o.weather.place.display == "Berlin, Germany"
    assert "berlin" in json.loads((tmp_path / "geocode.json").read_text(encoding="utf-8"))

    # failing twice is the offline card, after exactly one retry
    other = tmp_path / "other"
    other.mkdir()
    net = FakeNet(fail=overloaded())
    monkeypatch.setattr(weather, "fetch_json", net)
    o = outcome(make_ctx(other, online=True))
    assert net.urls() == [weather.GEOCODE_URL] * 2 and o.state == "offline"


def test_offline_mode_uses_any_cache_and_says_since_when(tmp_path: Path) -> None:
    seed_cache(tmp_path, "berlin", NOW - timedelta(days=2))
    ctx = make_ctx(tmp_path, online=False)
    o = outcome(ctx)
    assert o.state == "ok" and o.weather.offline
    assert meta_line(o.weather, ctx.now, Style(FONTS))[0] == "OFFLINE, DATA FROM SAT 21:15"
    # a stale reading is replaced by the forecast for this hour
    assert o.weather.conditions(ctx.now).time == datetime(2026, 10, 19, 21, 0)

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    seed_cache(fresh, "berlin", NOW - timedelta(minutes=5))
    o = outcome(make_ctx(fresh, online=False))
    assert not o.weather.offline
    assert meta_line(o.weather, BERLIN_NOW, Style(FONTS)) == ("UPDATED 21:10", BLACK)


def test_offline_without_cache_renders_the_offline_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(tmp_path, online=False)
    assert outcome(ctx).state == "offline"
    frame = frame_of(ctx)
    assert (frame == RED).sum() > 300

    # network on but failing: geocoded place cached, forecast never fetched
    (tmp_path / "geocode.json").write_text(
        json.dumps({"berlin": Place("Berlin", "Germany", 52.52, 13.41).to_dict()})
    )
    monkeypatch.setattr(weather, "fetch_json", FakeNet(fail=True))
    ctx = make_ctx(tmp_path, online=True)
    o = outcome(ctx)
    assert o.state == "offline" and o.place.display == "Berlin, Germany"
    frame_of(ctx)


def test_corrupt_cache_files_are_ignored_and_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "geocode.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "forecast.json").write_text("[1, 2", encoding="utf-8")
    assert outcome(make_ctx(tmp_path, online=False)).state == "offline"
    monkeypatch.setattr(weather, "fetch_json", city_net("berlin"))
    assert outcome(make_ctx(tmp_path, online=True)).state == "ok"
    assert "berlin" in json.loads((tmp_path / "geocode.json").read_text(encoding="utf-8"))
    assert outcome(make_ctx(tmp_path, online=False)).state == "ok"


def test_forecast_cache_round_trip_and_pruning(tmp_path: Path) -> None:
    data = fixture("forecast_berlin.json")
    weather.write_forecast_cache(tmp_path, "k0", data, NOW)
    got, stamp = weather.read_forecast_cache(tmp_path, "k0")
    assert got == data and stamp == NOW
    assert weather.read_forecast_cache(tmp_path, "missing") is None
    for i in range(1, 7):
        weather.write_forecast_cache(tmp_path, f"k{i}", {"daily": {}}, NOW + timedelta(minutes=i))
    kept = json.loads((tmp_path / "forecast.json").read_text(encoding="utf-8"))
    assert sorted(kept) == ["k3", "k4", "k5", "k6"]
    assert not list(tmp_path.glob("*.tmp"))  # atomic writes leave nothing behind


def test_fetch_json_maps_every_failure_to_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def respond(response: httpx.Response):
        def get(url: str, **kw: Any) -> httpx.Response:
            seen.update(kw)
            response.request = httpx.Request("GET", url)
            return response

        return get

    monkeypatch.setattr(weather.httpx, "get", respond(httpx.Response(200, json={"ok": 1})))
    assert weather.fetch_json(weather.FORECAST_URL, {"a": 1}, 4.0) == {"ok": 1}
    assert seen["headers"]["User-Agent"] == weather.USER_AGENT
    assert seen["timeout"].read == 4.0 and seen["params"] == {"a": 1}

    bad = httpx.Response(400, json={"error": True, "reason": "Latitude must be in range"})
    monkeypatch.setattr(weather.httpx, "get", respond(bad))
    with pytest.raises(weather.FetchError, match="HTTP 400: Latitude must be in range") as e:
        weather.fetch_json(weather.FORECAST_URL, {}, 4.0)
    assert e.value.transient is False

    monkeypatch.setattr(weather.httpx, "get", respond(httpx.Response(404, text="Not Found")))
    with pytest.raises(weather.FetchError, match="^HTTP 404$") as e:
        weather.fetch_json(weather.FORECAST_URL, {}, 4.0)
    assert e.value.transient is False

    busy = httpx.Response(503, text="The service is overloaded")
    monkeypatch.setattr(weather.httpx, "get", respond(busy))
    with pytest.raises(weather.FetchError, match="^HTTP 503$") as e:
        weather.fetch_json(weather.FORECAST_URL, {}, 4.0)
    assert e.value.transient is True

    monkeypatch.setattr(weather.httpx, "get", respond(httpx.Response(200, text="<html>")))
    with pytest.raises(weather.FetchError, match="not JSON") as e:
        weather.fetch_json(weather.FORECAST_URL, {}, 4.0)
    assert e.value.transient is False

    def down(url: str, **kw: Any) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(weather.httpx, "get", down)
    with pytest.raises(weather.FetchError, match="ConnectError") as e:
        weather.fetch_json(weather.FORECAST_URL, {}, 4.0)
    assert e.value.transient is True

    def loop(url: str, **kw: Any) -> httpx.Response:
        raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.")

    monkeypatch.setattr(weather.httpx, "get", loop)
    with pytest.raises(weather.FetchError, match="TooManyRedirects") as e:
        weather.fetch_json(weather.FORECAST_URL, {}, 4.0)
    assert e.value.transient is False  # a redirect loop does not fix itself in 2 s

    # the flag is keyword-only, defaults to transient and leaves the message alone
    assert weather.FetchError("x").transient is True
    err = weather.FetchError("HTTP 418", transient=False)
    assert str(err) == "HTTP 418" and err.transient is False
    ctx = RenderContext(settings={}, now=NOW, cache_dir=Path("."), fonts=FONTS)
    with pytest.raises(weather.FetchError, match="out of time") as e:
        weather._budget(ctx, time.monotonic())
    assert e.value.transient is False


@pytest.mark.parametrize("body", [{"results": "garbage"}, {"results": [{"name": "X"}]}, [1, 2]])
def test_malformed_geocoding_answers_are_an_offline_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: Any
) -> None:
    monkeypatch.setattr(weather, "fetch_json", lambda url, params, timeout: body)
    ctx = make_ctx(tmp_path, online=True)
    assert outcome(ctx).state in ("offline", "not_found")
    frame_of(ctx)
    assert not (tmp_path / "geocode.json").exists()


def test_error_bodies_are_never_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    net = FakeNet(forecast={"error": True, "reason": "Parameter 'current' is invalid"})
    monkeypatch.setattr(weather, "fetch_json", net)
    o = outcome(make_ctx(tmp_path, online=True, location="52.52,13.41"))
    assert o.state == "offline" and "invalid" in o.detail
    assert not (tmp_path / "forecast.json").exists()


# --- every card renders -----------------------------------------------------------------------


@pytest.mark.parametrize("city", sorted(CITIES))
@pytest.mark.parametrize("variant", [0, 1, 2])
def test_every_variant_renders_from_fixtures(tmp_path: Path, city: str, variant: int) -> None:
    seed_cache(tmp_path, city, NOW - timedelta(minutes=2))
    ctx = make_ctx(tmp_path, city, variant=variant)
    t = time.perf_counter()
    frame = frame_of(ctx)
    elapsed = time.perf_counter() - t
    assert elapsed < 3.0, f"render took {elapsed:.2f} s"
    assert (frame == BLACK).sum() > 3000  # type, rules, outlines
    assert (frame == WHITE).sum() > 100_000  # white is the paper
    assert (frame == RED).sum() < 2000  # red is an accent, never a fill
    if variant == 2:
        band = frame[:, :200]
        if city == "berlin":  # 21:15, clear: the night band
            assert (band == BLACK).mean() > 0.85 and (band == YELLOW).sum() > 3000
        else:  # overcast: the dotted band with a white cloud
            assert 0.03 < (band == BLACK).mean() < 0.5


@pytest.mark.parametrize(
    ("code", "is_day", "band_colour"),
    [(0, 1, YELLOW), (2, 1, YELLOW), (63, 1, BLUE), (95, 1, BLUE), (81, 0, BLUE), (1, 0, BLACK)],
)
def test_poster_band_colour_is_the_weather(
    tmp_path: Path, code: int, is_day: int, band_colour: int
) -> None:
    data = fixture("forecast_berlin.json")
    data["current"].update({"weather_code": code, "is_day": is_day})
    seed_cache(tmp_path, "berlin", NOW, data)
    frame = frame_of(make_ctx(tmp_path, variant=2))
    assert frame[5, 5] == band_colour and frame[395, 195] == band_colour
    assert band_for(code, bool(is_day)) in ("sun", "rain", "night")


def test_extreme_values_and_long_names_still_fit(tmp_path: Path) -> None:
    data = fixture("forecast_berlin.json")
    data["current"].update({"temperature_2m": -18.6, "apparent_temperature": -27.0})
    data["current"].update({"weather_code": 86, "wind_speed_10m": 88.4})
    for key in ("temperature_2m", "precipitation_probability"):
        data["hourly"][key] = [None] * len(data["hourly"]["time"])
    data["daily"]["precipitation_probability_max"][2] = None
    seed_cache(tmp_path, "berlin", NOW, data)
    long_name = "Llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch"
    geo = {"berlin": Place(long_name, "United Kingdom of Great Britain", 52.5244, 13.4105)}
    (tmp_path / "geocode.json").write_text(json.dumps({k: v.to_dict() for k, v in geo.items()}))
    for v in range(3):
        frame = frame_of(make_ctx(tmp_path, variant=v))
        # nothing is drawn into the outer margin columns (text was ellipsized, not clipped)
        if v != 2:
            assert (frame[:, :20] == WHITE).all() and (frame[:, -20:] == WHITE).all()
        else:
            assert (frame[:, -20:] == WHITE).all()


class TextLog(Canvas):
    """A canvas that remembers every string it draws and its bounding box."""

    def __init__(self, background: int = WHITE) -> None:
        super().__init__(background=background)
        self.texts: list[tuple[str, int, tuple[int, int, int, int]]] = []

    def text(self, xy, text, font, fill, anchor="la", threshold=128):  # type: ignore[override]
        box = super().text(xy, text, font, fill, anchor=anchor, threshold=threshold)
        self.texts.append((text, fill, box))
        return box


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


@pytest.mark.parametrize("layout", weather.LAYOUTS)
def test_the_longest_stamp_fits_every_layout(monkeypatch: pytest.MonkeyPatch, layout: str) -> None:
    """Imperial (12-hour clock) data from another day, with a long place name: the
    stamp is drawn in full, inside the margins, clear of every other text and glyph."""
    squares: list[tuple[float, float, float, float]] = []
    real = weather_layouts.draw_weather

    def spy(c: Canvas, code: Any, is_day: Any, cx: float, cy: float, size: int, **kw: Any):
        squares.append((cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2))
        return real(c, code, is_day, cx, cy, size, **kw)

    monkeypatch.setattr(weather_layouts, "draw_weather", spy)
    long_name = "Llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch"
    place = Place(long_name, "United Kingdom of Great Britain", 30.2672, -97.7431)
    data = fixture("forecast_austin_tx.json")
    w = parse_forecast(data, place, "imperial", NOW - timedelta(hours=40))  # Sat 22:15 local
    w.offline, w.frame_online = True, True
    style = Style(FONTS, imperial=True)
    stamp, fill = meta_line(w, AUSTIN_NOW, style)
    assert (stamp, fill) == ("COULDN'T UPDATE, DATA FROM SAT 10:15 PM", RED)

    c = TextLog()
    render = {
        "now": weather_layouts.render_now,
        "days": weather_layouts.render_days,
        "poster": weather_layouts.render_poster,
    }[layout]
    render(c, w, AUSTIN_NOW, style)
    drawn = [box for text, f, box in c.texts if text == stamp and f == RED]
    assert len(drawn) == 1, [t for t, _, _ in c.texts]  # verbatim, so not ellipsised
    box = drawn[0]
    assert M - 2 <= box[0] and box[2] <= W - M + 1
    others = [(t, b) for t, _, b in c.texts if t and t != stamp]
    assert any(t.upper().startswith("LLANFAIR") for t, _ in others)  # the place is there
    assert [t for t, b in others if _overlap(box, b)] == []
    assert [sq for sq in squares if _overlap(box, sq)] == []
    assert (c.to_frame()[:, -20:] == WHITE).all()


def test_an_old_cache_spreads_the_days_it_has_left(tmp_path: Path) -> None:
    """Two days after the fetch only three forecast days remain: they fill the
    width instead of leaving two empty columns."""
    seed_cache(tmp_path, "berlin", NOW)
    later = BERLIN_NOW + timedelta(days=2)
    o = outcome(make_ctx(tmp_path, now=later))
    assert o.weather.offline and [d.date.day for d in o.weather.next_days(later)] == [21, 22, 23]
    frame = frame_of(make_ctx(tmp_path, now=later, variant=1))
    glyph_row = frame[150:225]
    thirds = [glyph_row[:, x : x + 120] for x in (60, 240, 420)]
    assert all((t == BLACK).sum() > 300 for t in thirds)  # a glyph in every third
    # the hourly strip of an old cache starts from the current hour, not the fetch time
    hours = o.weather.next_hours(later)
    assert hours[0].time == datetime(2026, 10, 21, 22, 0)


def test_empty_location_card(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, None)
    assert outcome(ctx).state == "empty"
    frame = frame_of(ctx)
    assert (frame == RED).sum() > 200  # the compass needle
    # an app context without settings at all (e.g. a bare preview) is the same card
    bare = RenderContext(settings={}, now=BERLIN_NOW, cache_dir=tmp_path, fonts=FONTS)
    assert np.array_equal(render_to_frame(weather.APP, bare), frame)


def test_invalid_coordinates_card(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, location="95, 200")
    o = outcome(ctx)
    assert o.state == "invalid" and "latitude" in o.detail
    frame_of(ctx)


def test_variant_choice(tmp_path: Path) -> None:
    app = weather.APP
    s = schema_defaults(weather.SETTINGS_SCHEMA)
    base = make_ctx(tmp_path)
    assert app.pick_variant(base, s) == BERLIN_NOW.hour % 3
    hours = {
        app.pick_variant(make_ctx(tmp_path, now=BERLIN_NOW + timedelta(hours=h)), s)
        for h in range(3)
    }
    assert hours == {0, 1, 2}  # the rotation moves through the day
    assert app.pick_variant(make_ctx(tmp_path, seed=4), s) == 1
    assert app.pick_variant(make_ctx(tmp_path, seed=4), {**s, "layout": "poster"}) == 2
    assert app.pick_variant(make_ctx(tmp_path, seed=4, variant=0), {**s, "layout": "days"}) == 0

    seed_cache(tmp_path, "berlin", NOW)
    by_seed = render_to_frame(app, make_ctx(tmp_path, seed=5))
    assert np.array_equal(by_seed, render_to_frame(app, make_ctx(tmp_path, variant=2)))
    pinned = render_to_frame(app, make_ctx(tmp_path, layout="days"))
    assert np.array_equal(pinned, render_to_frame(app, make_ctx(tmp_path, variant=1)))


# --- model ------------------------------------------------------------------------------------


def test_parse_selects_the_right_hours_and_days() -> None:
    berlin = Place("Berlin", "Germany", 52.52, 13.41)
    w = parse_forecast(fixture("forecast_berlin.json"), berlin, "metric", NOW)
    assert [h.time.hour for h in w.next_hours(BERLIN_NOW)] == [22, 23, 0, 1, 2, 3]
    assert all(not h.is_day for h in w.next_hours(BERLIN_NOW))  # after the 18:02 sunset
    today = w.today(BERLIN_NOW)
    assert today is not None and fmt_temp(today.high) == "12°"
    assert [d.date.day for d in w.next_days(BERLIN_NOW)] == [19, 20, 21, 22, 23]
    assert w.current.temperature == 8.1 and w.current.is_day is False

    austin = Place("Austin", "Texas", 30.27, -97.74)
    w = parse_forecast(fixture("forecast_austin_tx.json"), austin, "imperial", NOW)
    assert [h.time.hour for h in w.next_hours(AUSTIN_NOW)] == [15, 16, 17, 18, 19, 20]
    # the same instant seen from Berlin picks Austin's local hours, not Berlin's
    assert w.next_hours(BERLIN_NOW) == w.next_hours(AUSTIN_NOW)
    assert w.current.temperature == 74.0


def test_parse_is_defensive() -> None:
    p = Place("X", "", 0, 0)
    data = fixture("forecast_berlin.json")
    data["hourly"]["temperature_2m"][22] = None
    data["daily"]["sunrise"] = None
    del data["current"]["relative_humidity_2m"]
    w = parse_forecast(data, p, "metric", NOW)
    assert w.next_hours(BERLIN_NOW)[0].temperature is None
    assert w.current.humidity is None and w.days[0].sunrise is None
    only_daily = {"daily": fixture("forecast_berlin.json")["daily"]}
    w = parse_forecast(only_daily, p, "metric", NOW)
    assert w.current.code == 3 and w.hours == []
    with pytest.raises(ValueError):
        parse_forecast({"latitude": 1}, p, "metric", NOW)
    with pytest.raises(ValueError):
        parse_forecast([], p, "metric", NOW)  # type: ignore[arg-type]


def test_formatting() -> None:
    assert fmt_temp(-3.6) == "−4°"
    assert fmt_temp(-0.4) == "0°"  # never "−0°"
    assert fmt_temp(2.5) == "3°" and fmt_temp(None) == "–"
    assert fmt_temp(71.0, degree=False) == "71"
    assert round_half_up(-2.5) == -3 and round_half_up(0.49) == 0
    assert fmt_clock(datetime(2026, 1, 1, 0, 5), twelve_hour=True) == "12:05 AM"
    assert fmt_clock(datetime(2026, 1, 1, 18, 2)) == "18:02"


# --- glyphs -----------------------------------------------------------------------------------


def test_every_wmo_code_has_a_glyph_and_words() -> None:
    for code in ALL_WMO:
        for is_day in (True, False):
            kind, intensity = glyph_for(code, is_day)
            assert kind in KINDS and 1 <= intensity <= 3
            assert condition_text(code, is_day) not in ("Unsettled", "No data")
    assert glyph_for(0, False)[0] == "clear-night"
    assert glyph_for(None)[0] == "cloudy" and glyph_for(42)[0] == "cloudy"
    assert condition_text(0, True) == "Sunny" and condition_text(0, False) == "Clear night"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("size", [32, 48, 140])
def test_glyphs_scale_and_stay_in_their_square(kind: str, size: int) -> None:
    for bg in (WHITE, YELLOW, BLUE, BLACK):
        c = Canvas(background=bg)
        cx, cy = 300, 200
        draw_glyph(c, kind, cx, cy, size, bg=bg)
        frame = c.to_frame()  # raises if anything off-palette slipped in
        ys, xs = np.nonzero(frame != bg)
        assert len(xs) > size * 2, f"{kind}@{size} on {bg} drew almost nothing"
        pad = 3
        assert xs.min() >= cx - size / 2 - pad and xs.max() <= cx + size / 2 + pad
        assert ys.min() >= cy - size / 2 - pad and ys.max() <= cy + size / 2 + pad
