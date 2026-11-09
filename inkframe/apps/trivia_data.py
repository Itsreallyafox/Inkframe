"""Data side of the Trivia app: the bundled facts, selection, the caches and
the Wikimedia "On this day" feed.

Kept apart from ``inkframe.apps.trivia`` (settings, selection policy, the
``App``) and ``inkframe.apps.trivia_layouts`` (drawing) so each file stays
readable.  Nothing here draws.

* The library is ``inkframe/apps/data/trivia.json``: about 300 facts written
  for the frame in our own words, each ``{"id", "text", "category",
  "source"}`` with the category one of ``CATEGORIES`` and the source a short
  note naming an institution or reference work.
* ``recent.json`` in the app's cache dir remembers the last ``RECENT_SIZE``
  fact ids so a refresh never repeats a fact shown recently.
* ``onthisday-MM-DD.json`` caches the day's Wikipedia "selected" events
  (``year`` and ``text`` only; the feed's page thumbnails are dropped).  One
  fetch per day: the file carries the full date, so last year's file for the
  same day is stale and refetched, and saving today's file removes the others.
  A transient failure (transport error or HTTP 5xx) is retried once after
  ``RETRY_DELAY`` seconds; any other failure, or offline mode, leaves the
  caller to fall back to the bundled facts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from inkframe import USER_AGENT
from inkframe.apps.quotes_data import clean_text, smarten

log = logging.getLogger("inkframe.apps.trivia")

DATA_FILE = Path(__file__).resolve().parent / "data" / "trivia.json"

#: the categories a bundled fact can carry (the ``categories`` setting filters by them)
CATEGORIES: tuple[str, ...] = (
    "science",
    "space",
    "nature",
    "animals",
    "history",
    "geography",
    "language",
    "numbers",
    "food",
)
#: a bundled fact's text length, in characters
MIN_FACT_CHARS = 40
MAX_FACT_CHARS = 180

RECENT_FILE = "recent.json"
RECENT_SIZE = 40

#: Wikimedia's keyless feed; "selected" is the short, curated list
ONTHISDAY_URL = (
    "https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/selected/{month:02d}/{day:02d}"
)
#: an event longer than this is dropped (the card has room for two or three)
MAX_EVENT_CHARS = 220
#: required on every card that shows Wikipedia text (CC BY-SA 4.0)
LICENCE = "Wikipedia · CC BY-SA 4.0"
DAY_CACHE_PREFIX = "onthisday-"
#: seconds to wait before the one retry of a transient failure
RETRY_DELAY = 2.0
#: the pause before a retry (tests replace it so nothing sleeps)
_sleep = time.sleep
#: cap on any single request, whatever ``http_timeout`` says
MAX_REQUEST_SECONDS = 8.0


class TriviaDataError(RuntimeError):
    """The bundled library is missing or unreadable."""


class FetchError(Exception):
    """The network, the HTTP status or the response body let us down.

    ``transient`` says whether trying again in a moment may help: True for a
    connection error, a timeout or an HTTP 5xx; False for a 4xx, any other
    request error (a redirect loop, a bad URL) or a body that is not JSON."""

    def __init__(self, *args: object, transient: bool = True) -> None:
        super().__init__(*args)
        self.transient = transient


@dataclass(frozen=True)
class Fact:
    id: str
    text: str
    category: str
    source: str = ""
    #: set for an "on this day" event shown as the card's fact
    year: int | None = None
    #: ``"bundled"`` or ``"onthisday"``
    origin: str = "bundled"


@dataclass(frozen=True)
class Event:
    year: int
    text: str


# --- the bundled library --------------------------------------------------------------

_LIBRARY_CACHE: dict[tuple[str, int, int], tuple[Fact, ...]] = {}


def normalise_category(name: str) -> str:
    return " ".join(name.split()).casefold()


def parse_library(data: Any) -> tuple[Fact, ...]:
    """Facts from the JSON document (a list, or ``{"facts": [...]}``).  Entries
    without text, with an unknown category or duplicating an earlier entry are
    skipped with a warning."""
    items = data.get("facts") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise TriviaDataError("trivia.json: expected a list of facts")
    facts: list[Fact] = []
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    for n, item in enumerate(items):
        if not isinstance(item, dict):
            log.warning("trivia.json entry %d is not an object; skipped", n)
            continue
        text = clean_text(str(item.get("text") or "")).replace("\n", " ")
        category = normalise_category(str(item.get("category") or ""))
        if not text:
            log.warning("trivia.json entry %d has no text; skipped", n)
            continue
        if category not in CATEGORIES:
            log.warning("trivia.json entry %d has unknown category %r; skipped", n, category)
            continue
        key = " ".join(text.casefold().split())
        fid = str(item.get("id") or "").strip() or (
            "fact-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
        )
        if fid in seen_ids or key in seen_texts:
            log.warning("trivia.json entry %d duplicates an earlier fact; skipped", n)
            continue
        seen_ids.add(fid)
        seen_texts.add(key)
        source = clean_text(str(item.get("source") or "")).replace("\n", " ")
        facts.append(Fact(fid, text, category, source))
    return tuple(facts)


def load_library(path: Path | None = None) -> tuple[Fact, ...]:
    """The bundled facts (cached until the file changes).  Raises
    ``TriviaDataError`` when the file is missing, unreadable or empty."""
    path = Path(path or DATA_FILE)
    try:
        st = path.stat()
    except OSError as e:
        raise TriviaDataError(f"{path.name} is missing") from e
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _LIBRARY_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise TriviaDataError(f"{path.name} is unreadable ({e.__class__.__name__})") from e
    facts = parse_library(data)
    if not facts:
        raise TriviaDataError(f"{path.name} has no usable facts")
    _LIBRARY_CACHE.clear()
    _LIBRARY_CACHE[key] = facts
    return facts


# --- selection ------------------------------------------------------------------------


def filter_pool(facts: Sequence[Fact], categories: Iterable[str]) -> list[Fact]:
    """The facts in the wanted ``categories`` (case does not matter); an empty
    list of categories means all of them.  Unknown names match nothing, so
    the pool can come back empty (the app renders a card for that)."""
    wanted = {normalise_category(c) for c in categories if c and c.strip()}
    if not wanted:
        return list(facts)
    return [f for f in facts if f.category in wanted]


def choose_fact(pool: Sequence[Fact], recent: Sequence[str], rng: random.Random) -> Fact:
    """A seeded pick from ``pool`` that avoids the ``recent`` ids (most recent
    last); with a pool smaller than the memory it only avoids the last one."""
    if not pool:
        raise ValueError("empty fact pool")
    recent_set = set(recent)
    candidates = [f for f in pool if f.id not in recent_set]
    if not candidates:
        last = recent[-1] if recent else None
        candidates = [f for f in pool if f.id != last] or list(pool)
    return rng.choice(candidates)


# --- small JSON caches -----------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        log.warning("trivia: ignoring unreadable cache %s: %s", path, e)
        return None


def _write_json(path: Path, data: Any) -> None:
    """Atomic write (temp file + replace); a failure is logged, not raised:
    the card still renders, it may just repeat a fact or refetch tomorrow's
    events today."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        log.warning("trivia: could not write %s: %s", path, e)


def load_recent(cache_dir: Path) -> list[str]:
    data = _read_json(Path(cache_dir) / RECENT_FILE)
    ids = data.get("ids") if isinstance(data, dict) else None
    if not isinstance(ids, list):
        return []
    return [i for i in ids if isinstance(i, str)][-RECENT_SIZE:]


def remember(cache_dir: Path, recent: Sequence[str], fid: str, now: datetime) -> list[str]:
    """Append ``fid`` (moving it to the end if present), keep the last
    ``RECENT_SIZE``, persist and return the new list."""
    ids = [i for i in recent if i != fid] + [fid]
    ids = ids[-RECENT_SIZE:]
    _write_json(
        Path(cache_dir) / RECENT_FILE,
        {"ids": ids, "updated_at": now.astimezone(timezone.utc).isoformat()},
    )
    return ids


# --- Wikimedia "On this day" ------------------------------------------------------------

#: "(pictured)", "(scene pictured)", "(both pictured)": the feed refers to
#: thumbnails the card does not show
_PICTURED = re.compile(r"\s*\([^()]*\bpictured\)")
_SPACES = re.compile(r"[\s  ]+")


def clean_event_text(text: str) -> str:
    text = _PICTURED.sub("", text)
    text = _SPACES.sub(" ", text).strip()
    return smarten(text)


def parse_onthisday(payload: Any) -> list[Event]:
    """``{"selected": [{"text", "year", "pages"}, ...]}`` -> ``[Event, ...]`` in
    the feed's order.  Falls back to ``events`` when ``selected`` is empty;
    drops items without a year or text, texts over ``MAX_EVENT_CHARS`` and
    duplicates.  Raises ``ValueError`` for a body that is not the feed."""
    if not isinstance(payload, dict):
        raise ValueError("unexpected on-this-day response")
    items = payload.get("selected")
    if not isinstance(items, list) or not items:
        items = payload.get("events")
    if not isinstance(items, list):
        raise ValueError("on-this-day response has no events")
    events: list[Event] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        year, text = item.get("year"), item.get("text")
        if isinstance(year, str) and year.strip().lstrip("-").isdigit():
            year = int(year)
        if isinstance(year, bool) or not isinstance(year, int) or not isinstance(text, str):
            continue
        text = clean_event_text(text)
        if not text or len(text) > MAX_EVENT_CHARS:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        events.append(Event(year, text))
    return events


def fetch_onthisday(
    month: int, day: int, timeout: float, *, transport: httpx.BaseTransport | None = None
) -> Any:
    """GET the day's feed and decode JSON.  The only function in the app that
    touches the network (tests replace it).  Raises ``FetchError``."""
    url = ONTHISDAY_URL.format(month=int(month), day=int(day))
    t = max(1.0, min(float(timeout), MAX_REQUEST_SECONDS))
    try:
        with httpx.Client(
            timeout=httpx.Timeout(t, connect=min(t, 4.0)),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        ) as client:
            r = client.get(url)
    except (httpx.HTTPError, httpx.InvalidURL, OSError) as e:
        raise FetchError(
            f"{type(e).__name__}: {e}", transient=isinstance(e, (httpx.TransportError, OSError))
        ) from None
    if r.status_code != 200:
        raise FetchError(f"HTTP {r.status_code}", transient=r.status_code >= 500)
    try:
        return r.json()
    except ValueError:
        raise FetchError("response is not JSON", transient=False) from None


def fetch_with_retry(month: int, day: int, timeout: float) -> Any:
    """``fetch_onthisday`` with at most one retry: a transient ``FetchError``
    is tried once more after ``RETRY_DELAY`` seconds."""
    try:
        return fetch_onthisday(month, day, timeout)
    except FetchError as e:
        if not e.transient:
            raise
        log.info("trivia: on-this-day fetch failed: %s; retrying once in %g s", e, RETRY_DELAY)
    _sleep(RETRY_DELAY)
    return fetch_onthisday(month, day, timeout)


def day_cache_path(cache_dir: Path, day: date) -> Path:
    return Path(cache_dir) / f"{DAY_CACHE_PREFIX}{day.month:02d}-{day.day:02d}.json"


def load_day_cache(cache_dir: Path, day: date) -> list[Event] | None:
    """The events cached for ``day`` (the full date must match), or ``None``."""
    data = _read_json(day_cache_path(cache_dir, day))
    if not isinstance(data, dict) or data.get("date") != day.isoformat():
        return None
    items = data.get("events")
    if not isinstance(items, list):
        return None
    events = []
    for item in items:
        if (
            isinstance(item, dict)
            and isinstance(item.get("year"), int)
            and isinstance(item.get("text"), str)
        ):
            events.append(Event(item["year"], item["text"]))
    return events


def save_day_cache(cache_dir: Path, day: date, events: Sequence[Event], now: datetime) -> None:
    """Write today's file and drop the other days' files (one is enough)."""
    cache_dir = Path(cache_dir)
    path = day_cache_path(cache_dir, day)
    _write_json(
        path,
        {
            "date": day.isoformat(),
            "fetched_at": now.astimezone(timezone.utc).isoformat(),
            "events": [{"year": e.year, "text": e.text} for e in events],
        },
    )
    try:
        for old in cache_dir.glob(f"{DAY_CACHE_PREFIX}*.json"):
            if old != path:
                old.unlink(missing_ok=True)
    except OSError as e:
        log.debug("trivia: could not prune old day caches: %s", e)


def onthisday_events(
    cache_dir: Path, now: datetime, *, online: bool, http_timeout: float
) -> list[Event] | None:
    """Today's events: from the day cache, else fetched (once, retried once)
    and cached.  ``None`` when nothing is available (offline with no cache,
    or the fetch failed): the caller falls back to the bundled facts and the
    card says nothing about it beyond its source line."""
    today = now.date()
    cached = load_day_cache(cache_dir, today)
    if cached is not None:
        return cached
    if not online:
        log.info("trivia: offline and no on-this-day cache for %s; using bundled facts", today)
        return None
    try:
        payload = fetch_with_retry(today.month, today.day, http_timeout)
        events = parse_onthisday(payload)
    except (FetchError, ValueError) as e:
        log.info("trivia: on-this-day unavailable (%s); using bundled facts", e)
        return None
    # an empty day is cached too, so a quiet feed is not asked again all day
    save_day_cache(cache_dir, today, events, now)
    return events


def event_fact(event: Event) -> Fact:
    """An "on this day" event dressed as a ``Fact`` for the single-fact layouts."""
    digest = hashlib.sha1(event.text.encode("utf-8")).hexdigest()[:8]
    return Fact(
        f"otd-{event.year}-{digest}",
        event.text,
        "on this day",
        "Wikipedia",
        year=event.year,
        origin="onthisday",
    )
