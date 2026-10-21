"""Weather data model: Open-Meteo JSON in, small typed records out.

Parsing is defensive: Open-Meteo answers with parallel arrays, any value may
be ``null``, and a cached file may come from an older run.  A missing value
becomes ``None`` and the layouts print a dash; only a response with no usable
current block *and* no daily block is rejected (``ValueError``).

Times: Open-Meteo is asked for ``timezone=auto``, so its timestamps are naive
local times *at the forecast location*, with ``utc_offset_seconds`` beside
them.  ``Weather.local_now`` converts the frame's clock into that zone, which
is what picks "the next six hours" and "today" correctly even when the frame
shows a city in another timezone.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

# --- WMO weather codes -------------------------------------------------------------

_CONDITIONS: dict[int, str] = {
    0: "Clear",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunder and hail",
    99: "Thunder and hail",
}


def condition_text(code: int | None, is_day: bool = True) -> str:
    """Short English condition for a WMO code ("Sunny", "Light rain", ...)."""
    if code is None:
        return "No data"
    c = int(code)
    if c == 0:
        return "Sunny" if is_day else "Clear night"
    if c == 1:
        return "Mostly sunny" if is_day else "Mostly clear"
    return _CONDITIONS.get(c, "Unsettled")


def band_for(code: int | None, is_day: bool = True) -> str:
    """The poster's colour band: ``sun``, ``rain``, ``night`` or ``plain``."""
    if code is None:
        return "plain"
    c = int(code)
    if c in (0, 1, 2):
        return "sun" if is_day else "night"
    if 51 <= c <= 67 or 80 <= c <= 82 or 95 <= c <= 99:
        return "rain"
    return "plain"


# --- records -------------------------------------------------------------------------


@dataclass(frozen=True)
class Place:
    """Where the forecast is for, as the user will read it."""

    name: str
    region: str
    latitude: float
    longitude: float

    @property
    def display(self) -> str:
        return f"{self.name}, {self.region}" if self.region else self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "region": self.region,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Place:
        return cls(
            name=str(d["name"]),
            region=str(d.get("region") or ""),
            latitude=float(d["latitude"]),
            longitude=float(d["longitude"]),
        )


@dataclass(frozen=True)
class Current:
    time: datetime | None
    temperature: float | None
    apparent: float | None
    humidity: float | None
    wind: float | None
    code: int | None
    is_day: bool


@dataclass(frozen=True)
class Hour:
    time: datetime
    temperature: float | None
    code: int | None
    precip: float | None
    is_day: bool


@dataclass(frozen=True)
class Day:
    date: date
    code: int | None
    high: float | None
    low: float | None
    precip: float | None
    sunrise: datetime | None
    sunset: datetime | None


@dataclass
class Weather:
    place: Place
    units: str
    #: when the data was fetched (aware, UTC)
    fetched_at: datetime
    utc_offset: timedelta
    current: Current
    hours: list[Hour] = field(default_factory=list)
    days: list[Day] = field(default_factory=list)
    #: True when the data is a stale cache shown because a refresh failed
    offline: bool = False
    #: whether the render was allowed to use the network (``ctx.online``, False only
    #: in offline mode, ``INKFRAME_OFFLINE=1``): False gives the "OFFLINE" stamp; True
    #: with ``offline`` means a refresh was tried and failed, whatever the cause (a
    #: lost Wi-Fi connection included), and gives "COULDN'T UPDATE"
    frame_online: bool = True

    # --- clocks -------------------------------------------------------------------
    def local_now(self, now: datetime) -> datetime:
        """``now`` (aware) as a naive time in the forecast location's zone."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (now.astimezone(timezone.utc) + self.utc_offset).replace(tzinfo=None)

    def local_time(self, t: datetime) -> datetime:
        """An aware instant as a naive time in the forecast location's zone."""
        return self.local_now(t)

    def age(self, now: datetime) -> timedelta:
        return now.astimezone(timezone.utc) - self.fetched_at

    # --- selections -----------------------------------------------------------------
    def today(self, now: datetime) -> Day | None:
        d = self.local_now(now).date()
        for day in self.days:
            if day.date == d:
                return day
        return None

    def next_hours(self, now: datetime, n: int = 6) -> list[Hour]:
        """The ``n`` hours after the current one (e.g. at 21:10: 22:00 ... 03:00)."""
        t = self.local_now(now).replace(minute=0, second=0, microsecond=0)
        return [h for h in self.hours if h.time > t][:n]

    def next_days(self, now: datetime, n: int = 5) -> list[Day]:
        d = self.local_now(now).date()
        return [day for day in self.days if day.date >= d][:n]

    def conditions(self, now: datetime) -> Current:
        """Current conditions; when the data is old (an offline cache), the
        temperature and sky come from the forecast for this hour instead of a
        reading taken hours ago."""
        cur = self.current
        if self.age(now) < timedelta(minutes=90):
            return cur
        t = self.local_now(now).replace(minute=0, second=0, microsecond=0)
        for h in self.hours:
            if h.time == t:
                return replace(
                    cur,
                    time=h.time,
                    temperature=h.temperature if h.temperature is not None else cur.temperature,
                    code=h.code if h.code is not None else cur.code,
                    is_day=h.is_day,
                )
        return cur


# --- parsing -------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any) -> int | None:
    f = _num(v)
    return None if f is None else int(f)


def _time(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v).replace(tzinfo=None)
    except ValueError:
        return None


def _date(v: Any) -> date | None:
    if not isinstance(v, str):
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def _column(block: Mapping[str, Any], key: str, n: int) -> list[Any]:
    col = block.get(key)
    if not isinstance(col, Sequence) or isinstance(col, str):
        return [None] * n
    col = list(col)[:n]
    return col + [None] * (n - len(col))


def parse_forecast(
    data: Mapping[str, Any], place: Place, units: str, fetched_at: datetime
) -> Weather:
    """Turn an Open-Meteo ``/v1/forecast`` response into a ``Weather``."""
    if not isinstance(data, Mapping):
        raise ValueError("forecast is not a JSON object")
    offset = timedelta(seconds=_num(data.get("utc_offset_seconds")) or 0)

    days: list[Day] = []
    daily = data.get("daily")
    if isinstance(daily, Mapping):
        times = daily.get("time") or []
        n = len(times) if isinstance(times, Sequence) else 0
        cols = {
            k: _column(daily, k, n)
            for k in (
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
                "sunrise",
                "sunset",
            )
        }
        for i in range(n):
            d = _date(times[i])
            if d is None:
                continue
            days.append(
                Day(
                    date=d,
                    code=_int(cols["weather_code"][i]),
                    high=_num(cols["temperature_2m_max"][i]),
                    low=_num(cols["temperature_2m_min"][i]),
                    precip=_num(cols["precipitation_probability_max"][i]),
                    sunrise=_time(cols["sunrise"][i]),
                    sunset=_time(cols["sunset"][i]),
                )
            )
    sun_by_date = {d.date: (d.sunrise, d.sunset) for d in days}

    def is_day_at(t: datetime) -> bool:
        rise, set_ = sun_by_date.get(t.date(), (None, None))
        if rise is None or set_ is None:
            return 6 <= t.hour < 18
        return rise <= t < set_

    hours: list[Hour] = []
    hourly = data.get("hourly")
    if isinstance(hourly, Mapping):
        times = hourly.get("time") or []
        n = len(times) if isinstance(times, Sequence) else 0
        temps = _column(hourly, "temperature_2m", n)
        codes = _column(hourly, "weather_code", n)
        precs = _column(hourly, "precipitation_probability", n)
        for i in range(n):
            t = _time(times[i])
            if t is None:
                continue
            hours.append(Hour(t, _num(temps[i]), _int(codes[i]), _num(precs[i]), is_day_at(t)))

    cur = data.get("current")
    if isinstance(cur, Mapping):
        ct = _time(cur.get("time"))
        is_day_raw = cur.get("is_day")
        if is_day_raw is None:
            is_day = is_day_at(ct) if ct else True
        else:
            is_day = bool(_int(is_day_raw))
        current = Current(
            time=ct,
            temperature=_num(cur.get("temperature_2m")),
            apparent=_num(cur.get("apparent_temperature")),
            humidity=_num(cur.get("relative_humidity_2m")),
            wind=_num(cur.get("wind_speed_10m")),
            code=_int(cur.get("weather_code")),
            is_day=is_day,
        )
    elif days:
        current = Current(None, None, None, None, None, days[0].code, True)
    else:
        raise ValueError("forecast has neither current conditions nor a daily forecast")

    return Weather(
        place=place,
        units=units,
        fetched_at=fetched_at.astimezone(timezone.utc),
        utc_offset=offset,
        current=current,
        hours=hours,
        days=days,
    )


# --- formatting -----------------------------------------------------------------------

MINUS = "\u2212"
DEGREE = "\u00b0"
DASH = "\u2013"
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def round_half_up(v: float) -> int:
    """Round to the nearest whole number, halves away from zero; never ``-0``."""
    r = int(math.floor(abs(v) + 0.5))
    return -r if v < 0 and r else r


def fmt_temp(v: float | None, degree: bool = True) -> str:
    """``-3.6`` -> ``"−4°"`` (a true minus sign); ``None`` -> ``"–"``."""
    if v is None:
        return DASH
    n = round_half_up(v)
    s = f"{MINUS}{abs(n)}" if n < 0 else str(n)
    return s + (DEGREE if degree else "")


def fmt_clock(t: datetime | None, twelve_hour: bool = False) -> str:
    if t is None:
        return DASH
    if twelve_hour:
        h = t.hour % 12 or 12
        return f"{h}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"
    return f"{t.hour:02d}:{t.minute:02d}"


def fmt_hour(t: datetime, twelve_hour: bool = False) -> str:
    """Hour label for the hourly strip: ``"22:00"`` or ``"10 PM"``."""
    if twelve_hour:
        h = t.hour % 12 or 12
        return f"{h} {'AM' if t.hour < 12 else 'PM'}"
    return f"{t.hour:02d}:00"


def fmt_day(d: date) -> str:
    return WEEKDAYS[d.weekday()]


def fmt_date(d: date) -> str:
    """``"MON 19 OCT"``."""
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"


def fmt_coords(lat: float, lon: float) -> str:
    """``52.52°N 13.41°E``."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat):.2f}{DEGREE}{ns} {abs(lon):.2f}{DEGREE}{ew}"
