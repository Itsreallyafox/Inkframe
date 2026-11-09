"""Countdown: the days until the next holiday, a birthday or a date of your own.

The card names an event, shows how many calendar days are left and carries a
small illustration per event: fireworks for New Year's Day, hearts for
Valentine's Day, shamrocks, a decorated Easter egg, a flag for Memorial Day,
stars and bursts for the 4th of July, a ribbon for Labor Day, a jack-o'-lantern,
autumn leaves and a pie, a Christmas tree, a cake with balloons for a birthday
and neutral confetti for a custom date.  Mother's Day and Father's Day are
left out on purpose (Adam's decision, docs/PLAN_0.2.md).

**Render path.** The motifs are full-colour Pillow drawings dithered by the
app itself with the real engine against ``ctx.measured_palette``
(``countdown_art``); the type is then set in flat palette colours with
``Canvas`` on top, so it stays crisp.  ``render`` therefore returns a
``(400, 600)`` palette-index array, and ``render_to_frame`` validates it and
skips its own dither pass (an index array is never dithered).
``needs_dither`` is ``True`` because the card carries dithered tone.

**Dates** are computed in the frame's local time from ``ctx.now``.  "Days
until" counts calendar days: at 23:59 the night before the card still says
1, on the day it says 0 and celebrates ("Merry Christmas!", no number), and
from the next day the count moves to the next occurrence.  Easter is the
Western date (the anonymous Gregorian computus).  A birthday on 02-29 falls
on Feb 28 in a non-leap year.

**Selection.** ``event = "auto"`` picks the nearest upcoming event (today
counts) among the holidays the ``holidays`` list allows, the birthday when
one is set and a yearly ``MM-DD`` custom date; a one-off ``YYYY-MM-DD``
custom date takes over until it has passed, then the automatic pick
resumes.  Any other ``event`` value pins that event.

**Layouts** (``countdown_layouts``): ``poster`` (the motif large, the number
very large), ``calendar`` (a tear-off calendar page) and ``banner`` (the motif
across the top).  ``layout = "rotate"`` changes the layout by the hour like
Weather.

**Failure states are cards, never errors.** A pinned birthday or custom
event with no date, a date that does not parse, an unknown key in
``holidays`` or an empty pool each render a friendly notice naming the
setting to fix.  ``validate(settings)`` raises the same messages as
``ValueError``; the settings API runs it (``CountdownApp.validate``), so a
bad value is refused with a 422 naming the field, and the cards only remain
for settings that reached the database some other way.
"""

from __future__ import annotations

import calendar
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np

from inkframe.apps import countdown_layouts as layouts
from inkframe.apps.base import Field, RenderContext, schema_defaults

log = logging.getLogger("inkframe.apps.countdown")

#: the built-in holidays, in calendar order, ``(key, label)``
HOLIDAYS: tuple[tuple[str, str], ...] = (
    ("new_year", "New Year's Day"),
    ("valentines", "Valentine's Day"),
    ("st_patricks", "St. Patrick's Day"),
    ("easter", "Easter"),
    ("memorial_day", "Memorial Day"),
    ("independence_day", "4th of July"),
    ("labor_day", "Labor Day"),
    ("halloween", "Halloween"),
    ("thanksgiving", "Thanksgiving"),
    ("christmas", "Christmas"),
)
HOLIDAY_KEYS: tuple[str, ...] = tuple(k for k, _ in HOLIDAYS)
HOLIDAY_LABELS: dict[str, str] = dict(HOLIDAYS)
#: every event key, holidays first
EVENT_KEYS: tuple[str, ...] = HOLIDAY_KEYS + ("birthday", "custom")

#: what the card says on the day itself (``{name}`` is the birthday name)
CELEBRATIONS: dict[str, str] = {
    "new_year": "Happy New Year!",
    "valentines": "Happy Valentine's Day!",
    "st_patricks": "Happy St. Patrick's Day!",
    "easter": "Happy Easter!",
    "memorial_day": "In remembrance",
    "independence_day": "Happy 4th of July!",
    "labor_day": "Happy Labor Day!",
    "halloween": "Happy Halloween!",
    "thanksgiving": "Happy Thanksgiving!",
    "christmas": "Merry Christmas!",
    "birthday": "Happy Birthday!",
    "custom": "Today's the day!",
}

DEFAULT_CUSTOM_LABEL = "The big day"
LAYOUTS: tuple[str, ...] = layouts.LAYOUT_NAMES

SETTINGS_SCHEMA: tuple[Field, ...] = (
    Field(
        "event",
        "Event",
        "select",
        "auto",
        options=(("auto", "Automatic (the next one)"),)
        + HOLIDAYS
        + (("birthday", "Birthday"), ("custom", "Custom date")),
        help="Automatic counts down to whichever allowed event comes next.",
    ),
    Field(
        "holidays",
        "Holidays",
        "list",
        list(HOLIDAY_KEYS),
        help="The holidays the automatic pick may choose, one key per line: "
        + ", ".join(HOLIDAY_KEYS)
        + ".",
    ),
    Field(
        "birthday",
        "Birthday",
        "text",
        "",
        help="MM-DD, e.g. 03-14; counted every year (02-29 falls on Feb 28 in other years).",
        placeholder="03-14",
    ),
    Field(
        "birthday_name",
        "Birthday name",
        "text",
        "",
        help="The card says “<Name>'s Birthday”; empty says “Your birthday”.",
        placeholder="Sam",
    ),
    Field(
        "custom_label",
        "Custom event",
        "text",
        "",
        help=f"What the custom date is for; empty says “{DEFAULT_CUSTOM_LABEL}”.",
        placeholder="Our trip",
    ),
    Field(
        "custom_date",
        "Custom date",
        "text",
        "",
        help="YYYY-MM-DD for a one-off date (it takes over until it has passed) "
        "or MM-DD for a yearly one.",
        placeholder="2027-06-12",
    ),
    Field(
        "layout",
        "Layout",
        "select",
        "rotate",
        help="Rotate shows a different layout each hour",
        options=(
            ("rotate", "Rotate"),
            ("poster", "Poster: the number very large"),
            ("calendar", "Calendar: a tear-off page"),
            ("banner", "Banner: the motif across the top"),
        ),
    ),
)


# --- date rules ------------------------------------------------------------------------


def easter(year: int) -> date:
    """Western (Gregorian) Easter Sunday: the anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Monday 0) of a month, e.g. the fourth Thursday."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + 7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    """The last ``weekday`` (Monday 0) of a month, e.g. the last Monday of May."""
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last.fromordinal(last.toordinal() - (last.weekday() - weekday) % 7)


def holiday_date(key: str, year: int) -> date:
    """The date of built-in holiday ``key`` in ``year``."""
    if key == "new_year":
        return date(year, 1, 1)
    if key == "valentines":
        return date(year, 2, 14)
    if key == "st_patricks":
        return date(year, 3, 17)
    if key == "easter":
        return easter(year)
    if key == "memorial_day":
        return last_weekday(year, 5, calendar.MONDAY)
    if key == "independence_day":
        return date(year, 7, 4)
    if key == "labor_day":
        return nth_weekday(year, 9, calendar.MONDAY, 1)
    if key == "halloween":
        return date(year, 10, 31)
    if key == "thanksgiving":
        return nth_weekday(year, 11, calendar.THURSDAY, 4)
    if key == "christmas":
        return date(year, 12, 25)
    raise ValueError(f"unknown holiday {key!r}")


def yearly_date(year: int, month: int, day: int) -> date:
    """``month``/``day`` in ``year``; Feb 29 becomes Feb 28 when the year has none."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)


def next_holiday(key: str, today: date) -> date:
    """The next occurrence of ``key`` on or after ``today`` (today counts)."""
    d = holiday_date(key, today.year)
    return d if d >= today else holiday_date(key, today.year + 1)


def next_yearly(month: int, day: int, today: date) -> date:
    d = yearly_date(today.year, month, day)
    return d if d >= today else yearly_date(today.year + 1, month, day)


def days_until(today: date, when: date) -> int:
    """Calendar days from ``today`` to ``when`` (0 on the day, negative after)."""
    return (when - today).days


# --- parsing the date settings ----------------------------------------------------------

_MONTH_DAY = re.compile(r"^(\d{1,2})-(\d{1,2})$")
_FULL_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def _check_month_day(field: str, month: int, day: int) -> None:
    if not 1 <= month <= 12:
        raise ValueError(f"{field}: the month must be 01-12, got {month:02d}")
    # a yearly date may be 02-29: it falls on Feb 28 in other years
    most = calendar.monthrange(2000, month)[1]
    if not 1 <= day <= most:
        raise ValueError(f"{field}: {calendar.month_name[month]} has {most} days, got {day:02d}")


def parse_month_day(field: str, text: str) -> tuple[int, int] | None:
    """``"MM-DD"`` -> ``(month, day)``; ``None`` for an empty string.  ``ValueError``
    naming ``field`` for anything else."""
    text = (text or "").strip()
    if not text:
        return None
    m = _MONTH_DAY.match(text)
    if not m:
        raise ValueError(f"{field}: use MM-DD, e.g. 03-14, got {text!r}")
    month, day = int(m.group(1)), int(m.group(2))
    _check_month_day(field, month, day)
    return month, day


@dataclass(frozen=True)
class CustomDate:
    """A parsed ``custom_date``: a one-off date, or a yearly month/day."""

    month: int
    day: int
    year: int | None = None

    @property
    def once(self) -> bool:
        return self.year is not None


def parse_custom_date(field: str, text: str) -> CustomDate | None:
    """``"YYYY-MM-DD"`` (once) or ``"MM-DD"`` (yearly); ``None`` when empty."""
    text = (text or "").strip()
    if not text:
        return None
    m = _FULL_DATE.match(text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not 1 <= year <= 9999:
            raise ValueError(f"{field}: the year must be 0001-9999, got {year}")
        _check_month_day(field, month, day)
        try:
            date(year, month, day)
        except ValueError:
            raise ValueError(f"{field}: {year} has no {month:02d}-{day:02d}") from None
        return CustomDate(month, day, year)
    if _MONTH_DAY.match(text):
        month, day = parse_month_day(field, text)  # type: ignore[misc]
        return CustomDate(month, day)
    raise ValueError(f"{field}: use YYYY-MM-DD or MM-DD, e.g. 2027-06-12, got {text!r}")


def check_holidays(keys: list[str]) -> list[str]:
    """The ``holidays`` list with duplicates dropped; ``ValueError`` naming an unknown key."""
    out: list[str] = []
    for key in keys:
        k = key.strip().lower()
        if k not in HOLIDAY_KEYS:
            raise ValueError(f"holidays: unknown event {key!r}; one of " + ", ".join(HOLIDAY_KEYS))
        if k not in out:
            out.append(k)
    return out


def validate(settings: Mapping[str, Any]) -> None:
    """Check the settings beyond the schema: the holiday keys and both date fields.
    Raises ``ValueError`` naming the field.  ``render`` turns the same problems
    into cards; this is for callers who want to refuse them up front."""
    check_holidays(list(settings.get("holidays") or []))
    parse_month_day("birthday", str(settings.get("birthday") or ""))
    parse_custom_date("custom_date", str(settings.get("custom_date") or ""))


# --- choosing the event ------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """What the card counts down to."""

    key: str
    label: str
    #: the occurrence being counted
    date: date
    #: calendar days from today; 0 on the day
    days: int
    celebration: str
    #: a one-off custom date (``YYYY-MM-DD``) rather than a yearly one
    once: bool = False

    @property
    def today(self) -> bool:
        return self.days == 0


@dataclass(frozen=True)
class Problem:
    """A settings problem, rendered as a friendly card."""

    field: str
    headline: str
    line: str
    footnote: str = ""


def birthday_label(name: str) -> str:
    name = (name or "").strip()
    return f"{name}'s Birthday" if name else "Your birthday"


def birthday_celebration(name: str) -> str:
    name = (name or "").strip()
    return f"Happy Birthday, {name}!" if name else CELEBRATIONS["birthday"]


def custom_label(settings: Mapping[str, Any]) -> str:
    return str(settings.get("custom_label") or "").strip() or DEFAULT_CUSTOM_LABEL


def _holiday_event(key: str, today: date) -> Event:
    when = next_holiday(key, today)
    return Event(key, HOLIDAY_LABELS[key], when, days_until(today, when), CELEBRATIONS[key])


def _birthday_event(settings: Mapping[str, Any], today: date) -> Event | Problem | None:
    """``None`` when no birthday is set."""
    try:
        md = parse_month_day("birthday", str(settings.get("birthday") or ""))
    except ValueError as e:
        return _bad_date("birthday", str(e))
    if md is None:
        return None
    when = next_yearly(md[0], md[1], today)
    name = str(settings.get("birthday_name") or "")
    return Event(
        "birthday",
        birthday_label(name),
        when,
        days_until(today, when),
        birthday_celebration(name),
    )


def _custom_event(settings: Mapping[str, Any], today: date) -> Event | Problem | None:
    """``None`` when no custom date is set.  A one-off date that has passed is an
    ``Event`` with negative ``days`` (the caller decides what to do with it)."""
    try:
        cd = parse_custom_date("custom_date", str(settings.get("custom_date") or ""))
    except ValueError as e:
        return _bad_date("custom_date", str(e))
    if cd is None:
        return None
    when = date(cd.year, cd.month, cd.day) if cd.year else next_yearly(cd.month, cd.day, today)
    return Event(
        "custom",
        custom_label(settings),
        when,
        days_until(today, when),
        CELEBRATIONS["custom"],
        once=cd.once,
    )


def _bad_date(field: str, message: str) -> Problem:
    detail = message.split(": ", 1)[1] if ": " in message else message
    return Problem(
        field,
        "Check the date",
        f"The {field.replace('_', ' ')} setting didn't parse",
        detail,
    )


def choose(settings: Mapping[str, Any], today: date) -> Event | Problem:
    """The event for ``today`` according to ``settings`` (validated or raw)."""
    pinned = str(settings.get("event") or "auto")
    if pinned in HOLIDAY_KEYS:
        return _holiday_event(pinned, today)
    if pinned == "birthday":
        ev = _birthday_event(settings, today)
        if ev is None:
            return Problem(
                "birthday",
                "When is your birthday?",
                "Set the date on the Apps page",
                "Apps > Countdown > Birthday, as MM-DD",
            )
        return ev
    if pinned == "custom":
        ev = _custom_event(settings, today)
        if ev is None:
            return Problem(
                "custom_date",
                f"When is {custom_label(settings).lower()}?"
                if settings.get("custom_label")
                else "What's the big day?",
                "Set the date on the Apps page",
                "Apps > Countdown > Custom date, as YYYY-MM-DD or MM-DD",
            )
        if isinstance(ev, Event) and ev.days < 0:
            return Problem(
                "custom_date",
                f"{ev.label} has passed",
                f"It was {ev.date:%B} {ev.date.day}, {ev.date.year}",
                "Set a new date in Apps > Countdown",
            )
        return ev
    return _automatic(settings, today)


def _automatic(settings: Mapping[str, Any], today: date) -> Event | Problem:
    try:
        allowed = check_holidays(list(settings.get("holidays") or []))
    except ValueError as e:
        return Problem(
            "holidays",
            "Check the holidays list",
            str(e).split(": ", 1)[1].split(";")[0],
            "Keys: " + ", ".join(HOLIDAY_KEYS),
        )
    birthday = _birthday_event(settings, today)
    if isinstance(birthday, Problem):
        return birthday
    custom = _custom_event(settings, today)
    if isinstance(custom, Problem):
        return custom
    if custom is not None and custom.once and custom.days >= 0:
        # a one-off date takes over until it has passed
        return custom
    pool: list[Event] = [_holiday_event(k, today) for k in HOLIDAY_KEYS if k in allowed]
    if birthday is not None:
        pool.append(birthday)
    if custom is not None and custom.days >= 0:
        pool.append(custom)  # a yearly custom date competes like a birthday
    if not pool:
        return Problem(
            "holidays",
            "Nothing to count down to",
            "Allow a holiday or set a birthday on the Apps page",
            "Apps > Countdown",
        )
    return min(pool, key=lambda ev: ev.days)


# --- the app -----------------------------------------------------------------------------


def pick_variant(ctx: RenderContext, settings: Mapping[str, Any]) -> int:
    """``ctx.variant`` wins, then a pinned layout setting, then the seed, then the
    hour (so the rotation changes through the day)."""
    n = len(LAYOUTS)
    if ctx.variant is not None:
        return int(ctx.variant) % n
    layout = settings.get("layout", "rotate")
    if layout in LAYOUTS:
        return LAYOUTS.index(layout)
    if ctx.seed is not None:
        return int(ctx.seed) % n
    return ctx.now.hour % n


def local_today(ctx: RenderContext) -> date:
    now = ctx.now if ctx.now.tzinfo is not None else ctx.now.astimezone()
    return now.date()


class CountdownApp:
    id = "countdown"
    name = "Countdown"
    description = "Days until the next holiday, your birthday or a date of your own"
    #: the motif carries dithered tone (drawn by the app itself; see the module docstring)
    needs_dither = True
    settings_schema = SETTINGS_SCHEMA
    variants = len(LAYOUTS)
    variant_names = LAYOUTS
    #: the settings API runs this after the schema check (``ValueError`` -> 422)
    validate = staticmethod(validate)

    def render(self, ctx: RenderContext) -> np.ndarray:
        settings = {**schema_defaults(SETTINGS_SCHEMA), **(ctx.settings or {})}
        today = local_today(ctx)
        picked = choose(settings, today)
        if isinstance(picked, Problem):
            log.info("countdown: %s (%s)", picked.headline, picked.field)
            return layouts.compose_problem(ctx.fonts, picked, today)
        variant = pick_variant(ctx, settings)
        frame, _number_box = layouts.compose(
            ctx.fonts, picked, variant, today, palette=ctx.measured_palette
        )
        return frame


APP = CountdownApp()


def describe(ctx_now: datetime, settings: Mapping[str, Any]) -> str:
    """One line for logs and tests: ``Christmas in 46 days (2026-12-25)``."""
    today = ctx_now.date()
    picked = choose(settings, today)
    if isinstance(picked, Problem):
        return f"problem: {picked.headline} ({picked.field})"
    return f"{picked.label} in {picked.days} days ({picked.date.isoformat()})"
