"""Weather: current conditions, the next hours and five days from Open-Meteo.

Data flow (plan section 8):

1. ``location`` is either ``"lat,lon"`` (used as is) or a place name, geocoded
   once through Open-Meteo's geocoding API and cached forever in
   ``cache_dir/geocode.json``.
2. The forecast is cached in ``cache_dir/forecast.json`` keyed by
   ``lat,lon,units`` with a ``fetched_at`` stamp.  A cache younger than
   ``CACHE_WINDOW`` is used without touching the network.
3. If a refresh fails (or ``ctx.online`` is False), any cached forecast is
   shown regardless of age with a red "offline, data from 13:50" line; with
   no cache at all the app renders a designed offline card.

The app never raises for network trouble or missing settings: every state
has a card.  ``fetch_json`` is the only function that touches the network,
so tests replace it.

Layouts live in ``weather_layouts``, glyphs in ``weather_glyphs`` and the
response parsing in ``weather_model``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from inkframe.apps.base import Field, RenderContext, schema_defaults
from inkframe.apps.draw import Canvas
from inkframe.apps.weather_layouts import (
    Style,
    render_days,
    render_notice,
    render_now,
    render_poster,
)
from inkframe.apps.weather_model import Place, Weather, fmt_coords, parse_forecast
from inkframe.display.palette import RED, WHITE

log = logging.getLogger("inkframe.apps.weather")

USER_AGENT = "Inkframe/0.1 (+https://github.com/inkframe)"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
#: reuse a cached forecast this long before asking again
CACHE_WINDOW = timedelta(minutes=15)
GEOCODE_FILE = "geocode.json"
FORECAST_FILE = "forecast.json"
#: forecasts kept in forecast.json (one per location/units the user tried)
MAX_FORECASTS = 4

CURRENT_VARS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day"
)
HOURLY_VARS = "temperature_2m,weather_code,precipitation_probability"
DAILY_VARS = (
    "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,"
    "precipitation_probability_max"
)

#: layout ids in variant order
LAYOUTS: tuple[str, ...] = ("now", "days", "poster")

SETTINGS_SCHEMA: tuple[Field, ...] = (
    Field(
        "location",
        "Location",
        "text",
        "",
        help="Place name, or latitude,longitude",
        placeholder="Berlin   or   52.52, 13.41",
        required=True,
    ),
    Field(
        "units",
        "Units",
        "select",
        "metric",
        options=(
            ("metric", "Metric: \u00b0C, km/h, 24-hour clock"),
            ("imperial", "Imperial: \u00b0F, mph, 12-hour clock"),
        ),
    ),
    Field(
        "layout",
        "Layout",
        "select",
        "rotate",
        help="Rotate shows a different layout each hour",
        options=(
            ("rotate", "Rotate"),
            ("now", "Now: big temperature and the next hours"),
            ("days", "Five days"),
            ("poster", "Poster"),
        ),
    ),
    Field(
        "label",
        "Display name",
        "text",
        "",
        help="Optional; shown instead of the geocoded name, e.g. Home",
        placeholder="Home",
    ),
)


class FetchError(Exception):
    """The network, the HTTP status or the response body let us down."""


# --- network ----------------------------------------------------------------------------


def fetch_json(url: str, params: dict[str, Any], timeout: float) -> Any:
    """GET ``url`` and decode JSON.  The only function in the app that touches the
    network (tests monkeypatch it).  Raises ``FetchError``."""
    try:
        r = httpx.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            follow_redirects=True,
        )
    except (httpx.HTTPError, httpx.InvalidURL, OSError) as e:
        raise FetchError(f"{type(e).__name__}: {e}") from None
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.status_code != 200:
        reason = body.get("reason") if isinstance(body, dict) else None
        raise FetchError(f"HTTP {r.status_code}" + (f": {reason}" if reason else ""))
    if body is None:
        raise FetchError("response is not JSON")
    return body


# --- cache files -------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("weather: ignoring unreadable cache %s (%s)", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic write (temp file + rename) so a crash never leaves half a cache."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as e:
        log.warning("weather: could not write cache %s (%s)", path, e)


def _parse_stamp(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# --- location -----------------------------------------------------------------------------

_COORDS = re.compile(r"^\s*([+-]?\d{1,3}(?:\.\d+)?)\s*[,;\s]\s*([+-]?\d{1,3}(?:\.\d+)?)\s*$")


def parse_coords(text: str) -> tuple[float, float] | None:
    """``"52.52, 13.41"`` -> ``(52.52, 13.41)``; ``None`` for a place name.
    Raises ``ValueError`` for coordinates out of range."""
    m = _COORDS.match(text)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("latitude must be -90..90 and longitude -180..180")
    return lat, lon


def _geo_key(name: str) -> str:
    return " ".join(name.split()).casefold()


#: countries whose cities are named by their state/province ("Austin, Texas")
_REGION_BY_ADMIN1 = {"US", "CA", "AU"}


def place_from_result(res: dict[str, Any]) -> Place:
    name = str(res.get("name") or "").strip()
    cc = str(res.get("country_code") or "").upper()
    admin1 = str(res.get("admin1") or "").strip()
    country = str(res.get("country") or "").strip()
    region = admin1 if cc in _REGION_BY_ADMIN1 and admin1 and admin1 != name else country
    return Place(name, region, float(res["latitude"]), float(res["longitude"]))


def geocode(name: str, ctx: RenderContext, deadline: float) -> Place | None:
    """Resolve a place name (cache first).  ``None`` means "no such place";
    ``FetchError`` means we could not ask."""
    path = ctx.cache_dir / GEOCODE_FILE
    cache = _read_json(path)
    key = _geo_key(name)
    hit = cache.get(key)
    if isinstance(hit, dict):
        try:
            return Place.from_dict(hit)
        except (KeyError, TypeError, ValueError):
            pass
    if not ctx.online:
        raise FetchError("offline")
    queries = [name]
    if "," in name:  # "Austin, TX": retry the bare name if the qualified one finds nothing
        queries.append(name.split(",", 1)[0].strip())
    for q in queries:
        body = fetch_json(
            GEOCODE_URL,
            {"name": q, "count": 1, "language": "en", "format": "json"},
            _budget(ctx, deadline),
        )
        results = body.get("results") if isinstance(body, dict) else None
        if results:
            try:
                place = place_from_result(results[0])
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                raise FetchError(f"unexpected geocoding result: {e!r}") from None
            entry = place.to_dict()
            entry["query"] = name
            entry["fetched_at"] = ctx.now.astimezone(timezone.utc).isoformat()
            cache[key] = entry
            _write_json(path, cache)
            return place
    return None


def _budget(ctx: RenderContext, deadline: float) -> float:
    left = deadline - time.monotonic()
    if left < 0.5:
        raise FetchError("out of time")
    return max(0.5, min(float(ctx.http_timeout), left))


# --- forecast -----------------------------------------------------------------------------


def forecast_params(lat: float, lon: float, units: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": CURRENT_VARS,
        "hourly": HOURLY_VARS,
        "daily": DAILY_VARS,
        "timezone": "auto",
        "forecast_days": 5,
    }
    if units == "imperial":
        params["temperature_unit"] = "fahrenheit"
        params["wind_speed_unit"] = "mph"
    return params


def forecast_key(lat: float, lon: float, units: str) -> str:
    return f"{lat:.4f},{lon:.4f},{units}"


def read_forecast_cache(cache_dir: Path, key: str) -> tuple[dict[str, Any], datetime] | None:
    entry = _read_json(cache_dir / FORECAST_FILE).get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("data"), dict):
        return None
    stamp = _parse_stamp(entry.get("fetched_at"))
    if stamp is None:
        return None
    return entry["data"], stamp


def write_forecast_cache(cache_dir: Path, key: str, data: dict[str, Any], at: datetime) -> None:
    path = cache_dir / FORECAST_FILE
    cache = _read_json(path)
    cache[key] = {"fetched_at": at.astimezone(timezone.utc).isoformat(), "data": data}
    if len(cache) > MAX_FORECASTS:  # keep the newest few
        ordered = sorted(cache.items(), key=lambda kv: str(kv[1].get("fetched_at", "")))
        cache = dict(ordered[-MAX_FORECASTS:])
    _write_json(path, cache)


def _check_forecast(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise FetchError("forecast is not a JSON object")
    if body.get("error"):
        raise FetchError(str(body.get("reason") or "forecast error"))
    if not isinstance(body.get("current"), dict) and not isinstance(body.get("daily"), dict):
        raise FetchError("forecast has no current or daily block")
    return body


@dataclass
class Outcome:
    """What ``load_weather`` found: a state for the card plus the data, if any."""

    #: ``ok`` | ``empty`` | ``not_found`` | ``invalid`` | ``offline``
    state: str
    weather: Weather | None = None
    query: str = ""
    place: Place | None = None
    detail: str = ""


def load_weather(ctx: RenderContext, settings: dict[str, Any]) -> Outcome:
    """Settings -> place -> forecast, using and maintaining the cache."""
    query = str(settings.get("location") or "").strip()
    label = str(settings.get("label") or "").strip()
    units = settings.get("units") or "metric"
    if not query:
        return Outcome("empty")
    deadline = time.monotonic() + max(1.0, float(ctx.http_timeout))

    try:
        coords = parse_coords(query)
    except ValueError as e:
        return Outcome("invalid", query=query, detail=str(e))
    if coords is not None:
        place = Place(label or fmt_coords(*coords), "", coords[0], coords[1])
    else:
        try:
            found = geocode(query, ctx, deadline)
        except FetchError as e:
            if ctx.online:
                log.warning("weather: geocoding %r failed: %s", query, e)
            return Outcome("offline", query=query, detail=str(e))
        if found is None:
            return Outcome("not_found", query=query)
        place = Place(label, "", found.latitude, found.longitude) if label else found

    key = forecast_key(place.latitude, place.longitude, units)
    cached = read_forecast_cache(ctx.cache_dir, key)
    now_utc = ctx.now.astimezone(timezone.utc)
    data: dict[str, Any] | None = None
    fetched_at = now_utc
    offline = False
    detail = ""
    if cached is not None:
        age = now_utc - cached[1]
        if timedelta(minutes=-5) <= age < CACHE_WINDOW:
            data, fetched_at = cached
    if data is None:
        try:
            if not ctx.online:
                raise FetchError("offline")
            body = fetch_json(
                FORECAST_URL,
                forecast_params(place.latitude, place.longitude, units),
                _budget(ctx, deadline),
            )
            data = _check_forecast(body)
            fetched_at = now_utc
            write_forecast_cache(ctx.cache_dir, key, data, fetched_at)
        except FetchError as e:
            detail = str(e)
            if ctx.online:
                log.warning("weather: forecast refresh failed: %s", e)
            if cached is None:
                return Outcome("offline", query=query, place=place, detail=detail)
            data, fetched_at = cached
            offline = True

    try:
        weather = parse_forecast(data, place, units, fetched_at)
    except ValueError as e:
        log.warning("weather: unusable forecast data: %s", e)
        return Outcome("offline", query=query, place=place, detail=str(e))
    weather.offline = offline
    return Outcome("ok", weather=weather, query=query, place=place, detail=detail)


# --- the app ------------------------------------------------------------------------------


class WeatherApp:
    id = "weather"
    name = "Weather"
    description = "Current conditions, the next hours and five days, from Open-Meteo"
    needs_dither = False
    settings_schema = SETTINGS_SCHEMA
    variants = len(LAYOUTS)

    def pick_variant(self, ctx: RenderContext, settings: dict[str, Any]) -> int:
        """``ctx.variant`` wins, then a pinned layout setting, then the seed,
        then the hour (so the rotation changes through the day)."""
        if ctx.variant is not None:
            return int(ctx.variant) % self.variants
        layout = settings.get("layout", "rotate")
        if layout in LAYOUTS:
            return LAYOUTS.index(layout)
        if ctx.seed is not None:
            return int(ctx.seed) % self.variants
        return ctx.now.hour % self.variants

    def render(self, ctx: RenderContext) -> Image.Image:
        settings = {**schema_defaults(SETTINGS_SCHEMA), **(ctx.settings or {})}
        style = Style(ctx.fonts, imperial=settings.get("units") == "imperial")
        outcome = load_weather(ctx, settings)
        c = Canvas(background=WHITE)
        if outcome.state == "ok" and outcome.weather is not None:
            variant = self.pick_variant(ctx, settings)
            (render_now, render_days, render_poster)[variant](c, outcome.weather, ctx.now, style)
            return c.to_image()
        render_notice_for(c, outcome, ctx, style)
        return c.to_image()


def render_notice_for(c: Canvas, o: Outcome, ctx: RenderContext, style: Style) -> None:
    now = ctx.now
    if o.state == "empty":
        render_notice(
            c,
            style,
            now,
            glyph="compass",
            headline="Where are you?",
            line="Set a location in Apps > Weather",
            footnote="A place name, or latitude,longitude",
        )
    elif o.state == "not_found":
        render_notice(
            c,
            style,
            now,
            glyph="pin",
            headline=f"No place called \u201c{o.query}\u201d",
            line="Check the location in Apps > Weather",
            footnote="Try a city name, or latitude,longitude",
        )
    elif o.state == "invalid":
        render_notice(
            c,
            style,
            now,
            glyph="pin",
            headline="Those coordinates are off the map",
            line="Use latitude,longitude, e.g. 52.52,13.41",
            footnote=o.detail,
        )
    else:  # offline with nothing cached
        place = o.place.display if o.place else o.query
        if ctx.online:
            line, foot = "Can't reach Open-Meteo", "Trying again at the next refresh"
        else:
            line, foot = "Offline, no forecast cached yet", "The forecast appears once back online"
        render_notice(
            c,
            style,
            now,
            glyph="offline",
            headline="No weather yet",
            line=line,
            place=place,
            footnote=foot,
            line_fill=RED,
        )


APP = WeatherApp()
