"""The rotation scheduler: the one and only thing that drives the panel.

Panel-care rules enforced here, in code (plan section 2; docs/SCHEDULER.md):

* **R1 floor**: two refreshes are never closer than
  ``panel.refresh_floor_seconds`` (never below 30 s), measured from the end
  of the last refresh *attempt*.  A failed attempt counts.
* **R2 show now**: the only bypass of R1; still waits
  ``panel.show_now_min_seconds`` after the last attempt.  One pending request;
  a newer one replaces it; ``fires_at`` is reported for the UI countdown.
* **R3 keep-alive**: the panel is refreshed at least every
  ``panel.keepalive_hours``, also while paused or in quiet hours, by
  re-showing the last frame (the rotation does not advance).
* **R4 power**: every refresh is ``init() -> [clear()] -> show() -> sleep()``
  with ``sleep()`` in a ``finally``.  Nothing else in the process touches the
  panel; previews render to PNG only.
* **R5 quiet hours**: no scheduled rotation refreshes; keep-alive and show-now
  still work.
* **R6 robustness**: a render that raises (anything, ``SystemExit``
  included) or returns an invalid frame is logged and its slot skipped
  before the panel is touched (the next enabled slot is tried a second
  later; if every slot fails the scheduler backs off for a minute); a
  display failure is logged and counts as an attempt, and the panel's
  content is then treated as unknown, so the next rotation change repaints
  it even with the same image.  The thread never dies and the panel is
  never blanked.
* **R7 persistence**: all timing anchors, the rotation position, the recent
  photos and the last frame survive a restart; a lost attempt anchor falls
  back to the last refresh, so a corrupt state row never shortens the floor.

Design: ``Scheduler.tick(now)`` is a pure-ish decision core (it looks at the
state and the settings, performs at most one refresh synchronously and
returns how long to sleep).  ``start()`` runs it on a daemon thread named
``scheduler`` that sleeps on an ``Event`` so ``wake()`` (settings changed,
show-now queued) re-evaluates immediately.  Tests never start the thread:
they call ``tick`` with a fake clock.

Clock safety: the Pi Zero has no RTC, so the wall clock can jump when NTP
syncs after boot.  The floor and the show-now minimum are measured on the
wall clock *and* on ``monotonic_fn`` (for attempts made by this process), so
no wall-clock step of any size shortens them.  ``tick`` also compares
wall-clock progress with ``monotonic_fn`` and shifts the timing anchors by
any jump larger than ``CLOCK_JUMP_SECONDS``: anchors this process set move
with the jump; anchors loaded from disk (written on a clock whose offset is
unknown, e.g. before a power cut) move only in the safe direction, so the
keep-alive is never postponed and the floor never shortened.  Anchors found
in the future (a restart with the clock behind) are pulled back to "now".

``INKFRAME_OFFLINE=1`` makes every render (scheduler and previews) use
``RenderContext.online = False``.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import os
import random
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

import numpy as np

from inkframe.apps.base import (
    App,
    FontLibrary,
    RenderContext,
    coerce_field,
    render_to_frame,
    schema_defaults,
    validate_settings,
)
from inkframe.config import (
    MIN_REFRESH_FLOOR_SECONDS,
    MIN_SHOW_NOW_SECONDS,
    QuietHours,
    Settings,
    SettingsStore,
)
from inkframe.db import Database, Photo
from inkframe.display.base import Display, DisplayError, validate_frame
from inkframe.display.palette import VALID_ROTATIONS, indices_to_image, pack_frame, unpack_frame
from inkframe.paths import DataPaths, atomic_write_bytes

log = logging.getLogger("inkframe.scheduler")

#: ``INKFRAME_OFFLINE=1`` (or true/yes/on) forbids network access in renders
OFFLINE_ENV_VAR = "INKFRAME_OFFLINE"

PHOTOS_APP_ID = "photos"
PHOTO_ORDERS: tuple[str, ...] = ("shuffle", "sequential")
DEFAULT_DWELL_MINUTES = 3
DEFAULT_PHOTO_ORDER = "shuffle"
MAX_DWELL_MINUTES = 24 * 60
#: shuffle avoids the last ``min(n - 1, SHUFFLE_MEMORY)`` photos shown
SHUFFLE_MEMORY = 20
#: how many recent photo ids are kept in the persisted state
RECENT_PHOTOS_KEPT = 50

#: ``tick`` never asks to sleep less than this ...
MIN_DELAY = 0.5
#: ... or more than this, so settings edits and clock jumps are seen within minutes
MAX_DELAY = 300.0
#: after a failed render: try the next slot this soon
RETRY_DELAY = 1.0
#: after every enabled slot failed to render in one pass
BACKOFF_SECONDS = 60.0
#: after an unexpected exception escaped ``tick`` (a bug)
CRASH_DELAY = 30.0
#: after a failed attempt, re-show the last good frame no sooner than this
#: (or the floor, whichever is longer): repairs a panel left white by a failed
#: clear-then-show without hammering a broken panel
REPAIR_RETRY_SECONDS = 300.0
#: wall-clock vs monotonic disagreement treated as a clock jump
CLOCK_JUMP_SECONDS = 60.0
#: the scheduler gives up waiting for one render after this long
RENDER_TIMEOUT_SECONDS = 180.0
#: a render is waited for in slices this long, so ``stop()`` is seen promptly
STOP_POLL_SECONDS = 0.5

#: db state keys: the scheduler's JSON blob, plus two mirrors Phase 1 reads
STATE_KEY = "scheduler"
LAST_REFRESH_KEY = "last_refresh_at"
LAST_SOURCE_KEY = "last_source"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: what a failing database read raises: sqlite errors, and ValueError/TypeError/
#: KeyError/IndexError for a row that does not parse (bad JSON, schema drift)
_DB_READ_ERRORS: tuple[type[BaseException], ...] = (
    sqlite3.Error,
    ValueError,
    TypeError,
    KeyError,
    IndexError,
)

_T = TypeVar("_T")


class RenderTimeout(RuntimeError):
    """A scheduler render took longer than ``render_timeout``, or an earlier
    render that did is still occupying the render thread."""


class _Unavailable(RuntimeError):
    """An item that cannot even be attempted (its data cannot be read); it
    fails like a render that raised, without calling the app."""


class _Stopping(RuntimeError):
    """``stop()`` was called while waiting for a render: not a render failure
    (nothing is recorded and the rotation does not move), just the end."""


#: ``_db_read`` default that tells "the row could not be read" from "no row"
_UNREADABLE: Any = object()


# --- small helpers ------------------------------------------------------------------


def offline_mode() -> bool:
    """True when ``INKFRAME_OFFLINE`` asks for network-free renders."""
    return os.environ.get(OFFLINE_ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def local_now() -> datetime:
    """The current local time, timezone-aware (the default ``now_fn``)."""
    return datetime.now().astimezone()


def _aware(t: datetime) -> datetime:
    """``t`` with a fixed-offset timezone; a naive value is taken as local time.

    A DST-aware zone (``zoneinfo``) is replaced by its offset at that instant,
    the same thing ``local_now()`` returns.  Python does arithmetic and
    comparisons between two datetimes that share one ``tzinfo`` on the *wall
    clock*, which across a DST change is an hour off; with fixed offsets the
    scheduler's timing maths always works on real instants.
    """
    if t.tzinfo is None:
        return t.astimezone()
    if isinstance(t.tzinfo, timezone):
        return t
    offset = t.utcoffset()
    if offset is None:  # a tzinfo that does not know its offset: treat as local
        return t.replace(tzinfo=None).astimezone()
    name = t.tzname()
    return t.astimezone(timezone(offset, name) if name else timezone(offset))


def iso(t: datetime | None) -> str | None:
    """ISO-8601 UTC with milliseconds (the db module's convention), or None.

    Rounded *up* to the next millisecond, so a persisted anchor is never
    earlier than the event: a floor read back after a restart is never short.
    """
    if t is None:
        return None
    try:
        t = t.astimezone(timezone.utc)
        rest = t.microsecond % 1000
        if rest:
            t += timedelta(microseconds=1000 - rest)
        return t.isoformat(timespec="milliseconds")
    except OverflowError:
        # a mangled anchor at the edge of datetime's range (year 9999): log it
        # as it is rather than let a log line take the scheduler down
        return t.isoformat()


def _parse_time(value: object) -> datetime | None:
    """Inverse of ``iso``; anything unparsable is None (naive is taken as UTC)."""
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)


def _clamp_delay(seconds: float) -> float:
    return float(min(MAX_DELAY, max(MIN_DELAY, seconds)))


def _is_ctrl_c(e: BaseException) -> bool:
    """A real Ctrl-C: a ``KeyboardInterrupt`` on the main thread (a test or a
    REPL driving ``tick``).  Signals reach only the main thread, so one in
    the scheduler thread was raised by some library, and is a failure."""
    return (
        isinstance(e, KeyboardInterrupt) and threading.current_thread() is threading.main_thread()
    )


def _app_of(source: Mapping[str, Any]) -> str | None:
    """The app id behind a refresh source (photos for a photo; None for a keep-alive)."""
    if source.get("type") == "photo":
        return PHOTOS_APP_ID
    if source.get("type") == "app":
        return str(source.get("id"))
    return None


# --- quiet hours ----------------------------------------------------------------------


def quiet_window_active(qh: QuietHours, now: datetime) -> tuple[bool, datetime | None]:
    """Is ``now`` inside a quiet window, and when does that window end?

    For each weekday ``d`` in ``qh.days`` a window starts on that weekday at
    ``qh.start`` and ends at ``qh.end`` the same day if ``end > start``, else
    the next day (``23:00``-``07:00`` listed for Sunday ends Monday 07:00).
    ``now`` is inside a window when ``start <= now < end``; only windows that
    started today or yesterday can contain it.  Times are compared on the wall
    clock of ``now``'s own timezone, so the result is right across DST
    changes; ``until`` is ``now`` plus the wall-clock time left (at worst an
    hour off on a DST night, which the scheduler's 5-minute re-check absorbs),
    and so is always after ``now``.  A DST-aware ``now`` (``zoneinfo``) is
    taken at its UTC offset at that instant, like ``local_now()`` returns it.

    Returns ``(False, None)`` when quiet hours are disabled or not active.
    """
    if not qh.enabled or not qh.days:
        return False, None
    now = _aware(now)
    start_t, end_t = qh.start_time(), qh.end_time()
    wall = now.replace(tzinfo=None)
    best: datetime | None = None
    for back in (0, 1):
        day = wall.date() - timedelta(days=back)
        if day.weekday() not in qh.days:
            continue
        start = datetime.combine(day, start_t)
        end_day = day if end_t > start_t else day + timedelta(days=1)
        end = datetime.combine(end_day, end_t)
        if start <= wall < end and (best is None or end > best):
            best = end
    if best is None:
        return False, None
    return True, now + (best - wall)


# --- photos slot options and order ---------------------------------------------------------


def photo_options(options: Mapping[str, Any] | None) -> tuple[timedelta, str]:
    """The photos slot's ``(dwell, order)``, tolerant of bad stored values
    (they fall back to the defaults: 3 minutes, shuffle)."""
    opts = options if isinstance(options, Mapping) else {}
    dwell = opts.get("dwell_minutes", DEFAULT_DWELL_MINUTES)
    if (
        isinstance(dwell, bool)
        or not isinstance(dwell, (int, float))
        or dwell != dwell  # NaN
        or not 1 <= dwell <= MAX_DWELL_MINUTES
    ):
        dwell = DEFAULT_DWELL_MINUTES
    order = opts.get("order", DEFAULT_PHOTO_ORDER)
    if not isinstance(order, str) or order not in PHOTO_ORDERS:
        order = DEFAULT_PHOTO_ORDER
    return timedelta(minutes=float(dwell)), order


def validate_photo_options(options: object) -> dict[str, Any]:
    """Strictly check a photos slot's options (API input) and return them with
    defaults filled: ``{"dwell_minutes": 1..1440 (int), "order": "shuffle" |
    "sequential"}``.  Raises ``ValueError`` naming the bad key."""
    if options is None:
        options = {}
    if not isinstance(options, Mapping):
        raise ValueError(f"options must be an object, got {type(options).__name__}")
    unknown = sorted(str(k) for k in options if k not in ("dwell_minutes", "order"))
    if unknown:
        raise ValueError(f"options has unknown key(s): {', '.join(unknown)}")
    dwell = options.get("dwell_minutes", DEFAULT_DWELL_MINUTES)
    if (
        isinstance(dwell, bool)
        or not isinstance(dwell, (int, float))
        or (isinstance(dwell, float) and not dwell.is_integer())
    ):
        raise ValueError(f"options.dwell_minutes must be a whole number of minutes, got {dwell!r}")
    dwell = int(dwell)
    if not 1 <= dwell <= MAX_DWELL_MINUTES:
        raise ValueError(
            f"options.dwell_minutes must be between 1 and {MAX_DWELL_MINUTES}, got {dwell}"
        )
    order = options.get("order", DEFAULT_PHOTO_ORDER)
    if not isinstance(order, str) or order not in PHOTO_ORDERS:
        raise ValueError(f"options.order must be one of {', '.join(PHOTO_ORDERS)}, got {order!r}")
    return {"dwell_minutes": dwell, "order": order}


def pick_next_photo(
    photos: Sequence[Photo],
    order: str,
    recent_ids: Sequence[str],
    rng: random.Random,
    *,
    current: str | None = None,
) -> Photo | None:
    """Choose the next photo to show, or None when there are no photos.

    * ``sequential`` walks the photos by ``(created_at, id)``, cycling, from
      the most recent id in ``recent_ids`` that still exists (so a deleted
      photo does not restart the walk).
    * ``shuffle`` (and any unknown order) picks uniformly among the photos not
      in the last ``min(len(photos) - 1, 20)`` entries of ``recent_ids``.

    ``current`` (the photo on the panel) is never picked while another photo
    exists, so a dwell always changes the picture.
    """
    if not photos:
        return None
    ordered = sorted(photos, key=lambda p: (p.created_at, p.id))
    if len(ordered) == 1:
        return ordered[0]
    if order == "sequential":
        position = {p.id: i for i, p in enumerate(ordered)}
        anchor = next((position[r] for r in reversed(recent_ids) if r in position), None)
        i = 0 if anchor is None else (anchor + 1) % len(ordered)
        if ordered[i].id == current:
            i = (i + 1) % len(ordered)
        return ordered[i]
    k = min(len(ordered) - 1, SHUFFLE_MEMORY)
    avoid = set(list(recent_ids)[-k:]) if k > 0 else set()
    if current is not None:
        avoid.add(current)
    candidates = [p for p in ordered if p.id not in avoid]
    if not candidates:
        candidates = [p for p in ordered if p.id != current] or ordered
    return rng.choice(candidates)


# --- render contexts (shared with the preview endpoint) ----------------------------------------


def app_settings_for(app: App, db: Database) -> dict[str, Any]:
    """The app's complete, validated settings (stored values over the schema
    defaults).  Stored values that no longer validate (a schema change) fall
    back to their defaults field by field instead of disabling the app."""
    stored = db.get_app_settings(app.id)
    try:
        return validate_settings(app.settings_schema, stored)
    except ValueError as e:
        log.warning("app %s: stored settings do not validate (%s); using defaults", app.id, e)
    out = schema_defaults(app.settings_schema)
    for f in app.settings_schema:
        if f.key in stored:
            try:
                out[f.key] = coerce_field(f, stored[f.key])
            except ValueError:
                pass
    return out


def build_render_context(
    app: App,
    *,
    settings: Settings,
    db: Database,
    paths: DataPaths,
    now: datetime,
    fonts: FontLibrary | None = None,
    online: bool = True,
    item_id: str | None = None,
    variant: int | None = None,
    seed: int | None = None,
) -> RenderContext:
    """The ``RenderContext`` the scheduler and the preview endpoint give an app.

    ``online`` is forced off by ``INKFRAME_OFFLINE``; ``cache_dir`` is
    ``<data>/cache/<app_id>`` (created).
    """
    cache_dir = paths.cache / app.id
    cache_dir.mkdir(parents=True, exist_ok=True)
    return RenderContext(
        settings=app_settings_for(app, db),
        now=_aware(now),
        cache_dir=cache_dir,
        fonts=fonts if fonts is not None else FontLibrary(),
        variant=variant,
        seed=seed,
        online=bool(online) and not offline_mode(),
        measured_palette=settings.measured_palette(),
        db=db,
        paths=paths,
        app_settings=settings,
        item_id=item_id,
    )


# --- persisted state ------------------------------------------------------------------


_TIME_FIELDS = (
    "slot_started_at",
    "photo_started_at",
    "last_refresh_at",
    "last_attempt_at",
    "created_at",
)
#: the anchors the keep-alive deadline is measured from (R3)
_KEEPALIVE_ANCHORS = ("last_refresh_at", "created_at")
#: the anchor the floor and the show-now minimum are measured from (R1, R2)
_ATTEMPT_ANCHOR = "last_attempt_at"


@dataclass
class SchedulerState:
    """Everything the scheduler remembers across restarts (db key ``scheduler``).

    Times are aware datetimes in memory and ISO-8601 UTC strings on disk.
    """

    #: index into ``settings.rotation`` (all slots, enabled or not)
    slot_index: int = 0
    #: app id of that slot when it started, to follow it through rotation edits
    slot_app_id: str | None = None
    #: when the slot's first frame appeared; None = the slot has not shown yet
    slot_started_at: datetime | None = None
    #: the photo the photos slot is showing (None: none, or not a photos slot)
    photo_id: str | None = None
    photo_started_at: datetime | None = None
    #: photo ids in the order they were picked (shown or failed), newest last
    recent_photo_ids: list[str] = field(default_factory=list)
    #: the shuffle pick reserved for the next photo (so "next" is stable)
    next_photo_id: str | None = None
    #: end of the last successful refresh
    last_refresh_at: datetime | None = None
    #: end of the last refresh attempt (also written when the panel is first
    #: touched, so a crash or power cut mid-refresh still counts for the floor)
    last_attempt_at: datetime | None = None
    #: what the last successful refresh showed: {type: app|photo|keepalive, id, label}
    last_source: dict[str, Any] | None = None
    #: the content on the panel ({type: app|photo, id, label}); a keep-alive keeps it
    showing: dict[str, Any] | None = None
    #: the panel may not show ``showing`` (a refresh attempt failed or was cut
    #: short, e.g. white after ``clear()``; or ``invalidate()`` was called), so
    #: the rotation must not skip a refresh of the same content as "already
    #: showing"; cleared by the next successful refresh of a fresh render (a
    #: keep-alive re-show of the last frame leaves it set)
    panel_stale: bool = False
    #: the most recent failure: {at, kind: render|display, app_id, message}
    last_error: dict[str, Any] | None = None
    #: packing rotation ``last_frame.bin`` was written with
    frame_rotation: int | None = None
    #: when this state was first created (keep-alive anchor if nothing was shown)
    created_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        for name in _TIME_FIELDS:
            d[name] = iso(getattr(self, name))
        return d

    @classmethod
    def from_json(cls, data: object) -> SchedulerState:
        """Tolerant parse: every missing or malformed field takes its default."""
        st = cls()
        if not isinstance(data, Mapping):
            return st
        idx = data.get("slot_index")
        if isinstance(idx, int) and not isinstance(idx, bool) and idx >= 0:
            st.slot_index = idx
        for name in ("slot_app_id", "photo_id", "next_photo_id"):
            v = data.get(name)
            if isinstance(v, str) and v:
                setattr(st, name, v)
        for name in _TIME_FIELDS:
            setattr(st, name, _parse_time(data.get(name)))
        recent = data.get("recent_photo_ids")
        if isinstance(recent, list):
            st.recent_photo_ids = [r for r in recent if isinstance(r, str)][-RECENT_PHOTOS_KEPT:]
        for name in ("last_source", "showing", "last_error"):
            v = data.get(name)
            if isinstance(v, dict):
                setattr(st, name, dict(v))
        rot = data.get("frame_rotation")
        if rot in VALID_ROTATIONS:
            st.frame_rotation = int(rot)
        stale = data.get("panel_stale")
        if isinstance(stale, bool):
            st.panel_stale = stale
        return st


# --- internal value types --------------------------------------------------------------------


@dataclass
class _Pending:
    """One queued "show now" request (kept in memory only: a restart drops it)."""

    kind: str  # "next" | "app" | "photo"
    id: str | None
    label: str
    #: for the log; ``fires_at`` is derived from the last attempt, not from this
    requested_at: datetime
    #: an app show-now may force one of the app's layout variants
    variant: int | None = None


@dataclass(frozen=True)
class _Item:
    """Something to show and how it moves the rotation.

    ``mode``: ``enter`` starts a visit of ``slot_index``; ``photo`` changes the
    photo inside the current (started) photos slot; ``keep`` leaves the
    rotation timing alone (show-now of something outside the rotation, or a
    keep-alive re-render).  ``error`` is set when the item cannot be rendered
    at all (the photo list is unreadable): it fails like a render that raised.
    ``variant`` forces the app's layout variant (``RenderContext.variant``).
    """

    slot_index: int | None
    app_id: str
    photo: Photo | None
    mode: str
    error: str | None = None
    variant: int | None = None


@dataclass(frozen=True)
class _RotationPlan:
    due_at: datetime
    item: _Item
    reason: str


@dataclass(frozen=True)
class _Plan:
    action: str  # "wait" | "pending" | "keepalive" | "rotate"
    reason: str
    until: datetime | None = None
    item: _Item | None = None
    origin: str = "rotation"  # for "rotate": "rotation" | "keepalive"
    #: for "rotate": the keep-alive is due too, so a rotation item that would
    #: not refresh the panel (already showing) re-shows the last frame instead
    keepalive_due: bool = False


# --- the scheduler -------------------------------------------------------------------------


class Scheduler:
    """Rotation, durations, dwell, quiet hours, keep-alive, floor, show-now.

    Thread-safe: the public control methods (``show_now``, ``show_next``,
    ``pause``, ``resume``, ``wake``, ``snapshot``) may be called from API
    handler threads while the scheduler thread runs ``tick``.  The state lock
    is never held during a render or a panel refresh.
    """

    def __init__(
        self,
        display: Display,
        settings_store: SettingsStore,
        db: Database,
        paths: DataPaths,
        apps: Mapping[str, App],
        *,
        now_fn: Callable[[], datetime] | None = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
        fonts: FontLibrary | None = None,
        online: bool = True,
        executor: Executor | None = None,
        rng: random.Random | None = None,
        render_timeout: float = RENDER_TIMEOUT_SECONDS,
    ) -> None:
        """``now_fn`` returns aware local time (default: the system clock);
        ``executor`` (the app's one-thread render executor) serialises the
        scheduler's renders with the previews, which keeps fonts and the
        dither engine single-threaded; without it renders run inline."""
        self._display = display
        self._store = settings_store
        self._db = db
        self._paths = paths
        self._apps: dict[str, App] = dict(apps)
        self._now_fn = now_fn or local_now
        self._monotonic = monotonic_fn
        self._fonts = fonts if fonts is not None else FontLibrary()
        self._online = bool(online)
        self._executor = executor
        self._rng = rng or random.Random()
        self._render_timeout = float(render_timeout)

        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._pending: _Pending | None = None
        self._failed_slots: set[int] = set()
        self._backoff_until: datetime | None = None
        self._clock_ref: tuple[datetime, float] | None = None
        self._last_frame: np.ndarray | None = None
        self._bad_frame_file: tuple[int, int] | None = None
        self._warned_unknown: set[str] = set()
        #: ``monotonic_fn()`` at the end of this process's last refresh attempt
        self._attempt_mono: float | None = None
        #: keep-alive/attempt anchors this process set itself (vs. loaded from
        #: disk or clamped), i.e. known to be on the clock of the last tick
        self._live: set[str] = set()
        #: True while ``tick`` renders or refreshes (for reporting only)
        self._busy = False
        #: True from just before ``_refresh`` checks the stop flag until the
        #: panel is asleep again (see ``refreshing``)
        self._refreshing = False
        #: a render that timed out but still occupies the render thread
        self._stuck_render: Future[np.ndarray] | None = None
        self._stuck_label = ""
        #: database reads that are failing now -> the error, so each failure
        #: is logged once (with its traceback) and its recovery once
        self._db_failing: dict[str, str] = {}

        with self._lock:
            self._state = self._load_state(_aware(self._now_fn()))

    # --- public control API -------------------------------------------------------------

    @property
    def running(self) -> bool:
        """True while the scheduler thread is alive."""
        t = self._thread
        return t is not None and t.is_alive()

    @property
    def refreshing(self) -> bool:
        """True while the scheduler is driving the panel (``init`` to
        ``sleep``), or is about to.  Once ``stop()`` has been called and this
        is False, the scheduler will not touch the panel again, so shutdown
        may put the display to sleep and close it without cutting a refresh
        short (``_refresh`` sets the flag before it checks the stop flag)."""
        return self._refreshing

    def start(self) -> None:
        """Start the scheduler thread (a daemon named ``scheduler``).  Idempotent."""
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 45.0) -> bool:
        """Stop the thread and join it.  A refresh in flight finishes first (it
        cannot be interrupted without leaving the panel powered); no new one
        starts.  Returns False if the thread is still alive after ``timeout``."""
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is None:
            return True
        t.join(timeout)
        if t.is_alive():
            log.warning("scheduler thread did not stop within %.0f s", timeout)
            return False
        self._thread = None
        return True

    def wake(self) -> None:
        """Make the thread re-evaluate now (settings changed, request queued)."""
        self._wake.set()

    def pause(self) -> None:
        """Pause the rotation (persisted in ``settings.paused``).  Keep-alive and
        show-now keep working."""
        self._store.update(lambda s: s if s.paused else dataclasses.replace(s, paused=True))
        log.info("rotation paused")
        self.wake()

    def resume(self) -> None:
        """Resume the rotation."""
        self._store.update(lambda s: dataclasses.replace(s, paused=False) if s.paused else s)
        log.info("rotation resumed")
        self.wake()

    def show_next(self) -> dict[str, Any]:
        """Queue a show-now of the next rotation item; returns the pending
        request as ``snapshot()`` reports it."""
        return self._queue(_Pending("next", None, "Next", _aware(self._now_fn())))

    def show_now(
        self,
        app_id: str | None = None,
        photo_id: str | None = None,
        *,
        variant: int | None = None,
    ) -> dict[str, Any]:
        """Queue a show-now of an app or a photo (exactly one of the two).

        ``variant`` (apps only) forces one of the app's layout variants for
        this showing, e.g. the patterns app's palette test card.

        Raises ``ValueError`` unless exactly one is given (or for a variant
        that is out of range or given with a photo) and ``KeyError`` for an
        unknown app or photo id.  Excluded photos may still be shown
        explicitly.  A newer request replaces an older pending one.
        """
        if (app_id is None) == (photo_id is None):
            raise ValueError("pass exactly one of app_id or photo_id")
        if variant is not None and app_id is None:
            raise ValueError("variant goes with app_id only")
        now = _aware(self._now_fn())
        if app_id is not None:
            app = self._apps.get(app_id)
            if app is None:
                raise KeyError(f"unknown app {app_id!r}")
            if variant is not None:
                count = int(getattr(app, "variants", 1))
                if isinstance(variant, bool) or not isinstance(variant, int):
                    raise ValueError(f"variant must be a whole number, got {variant!r}")
                if not 0 <= variant < count:
                    raise ValueError(f"variant must be 0..{count - 1} for {app_id}, got {variant}")
            return self._queue(_Pending("app", app_id, app.name, now, variant))
        assert photo_id is not None
        photo = self._db.get_photo(photo_id)
        if photo is None:
            raise KeyError(f"unknown photo {photo_id!r}")
        return self._queue(_Pending("photo", photo.id, photo.original_name, now))

    def current_photo_id(self) -> str | None:
        """The photo the photos slot is showing (or last showed), if any."""
        with self._lock:
            return self._state.photo_id

    def invalidate(self, photo_id: str | None = None) -> bool:
        """The content on the panel would render differently now (for
        example a new recipe for the photo on it): the next rotation change
        refreshes the panel even when it shows the same photo again, instead
        of skipping it as "already showing".  With ``photo_id``, only when
        that photo is on the panel.  Nothing is refreshed early; the floor
        and quiet hours still apply.  Returns True if the panel was marked."""
        with self._lock:
            showing = self._state.showing or {}
            if photo_id is not None and (showing.get("type"), showing.get("id")) != (
                "photo",
                photo_id,
            ):
                return False
            if not self._state.panel_stale:
                self._state.panel_stale = True
                self._save()
            return True

    def snapshot(self) -> dict[str, Any]:
        """The scheduler's part of ``/api/status`` (all timestamps ISO UTC):

        ``{"panel": {state, last_refresh_at, last_attempt_at, last_source,
        last_error}, "rotation": {paused, quiet, quiet_until, current, next,
        next_at, keepalive_at, pending}}``
        """
        now = _aware(self._now_fn())
        mono = self._monotonic()
        settings = self._store.get()
        state = getattr(self._display, "state", None)
        if state not in ("asleep", "awake", "refreshing"):
            state = "awake" if self._display.awake else "asleep"
        with self._lock:
            st = self._state
            quiet, quiet_until = quiet_window_active(settings.quiet_hours, now)
            plan = self._rotation_plan(now, mono, settings)
            current = self._current_info(settings)
            next_at = self._next_rotation_at(now, mono, settings, plan)
            keepalive_at = self._keepalive_deadline(settings)
            floor_at = self._floor_at(settings, now, mono)
            if floor_at is not None and floor_at > keepalive_at:
                keepalive_at = floor_at
            if keepalive_at < now:
                keepalive_at = now
            return {
                "panel": {
                    "state": state,
                    "last_refresh_at": iso(st.last_refresh_at),
                    "last_attempt_at": iso(st.last_attempt_at),
                    "last_source": dict(st.last_source) if st.last_source else None,
                    "last_error": dict(st.last_error) if st.last_error else None,
                },
                "rotation": {
                    "paused": settings.paused,
                    "quiet": quiet,
                    "quiet_until": iso(quiet_until),
                    "current": current,
                    "next": self._item_info(plan.item) if plan is not None else None,
                    "next_at": iso(next_at),
                    "keepalive_at": iso(keepalive_at),
                    "pending": self._pending_info(settings, now, mono),
                },
            }

    # --- the thread ----------------------------------------------------------------------

    def _run(self) -> None:
        log.info("scheduler thread started")
        while not self._stop.is_set():
            try:
                delay = self.tick(self._now_fn())
            except BaseException as e:  # a bug, even a SystemExit, must not kill the panel's driver
                if _is_ctrl_c(e):
                    raise
                log.exception("scheduler tick failed; retrying in %.0f s", CRASH_DELAY)
                delay = CRASH_DELAY
            if self._stop.is_set():
                break
            self._wake.wait(delay)
            self._wake.clear()
        log.info("scheduler thread stopped")

    # --- the decision core -----------------------------------------------------------------

    def tick(self, now: datetime) -> float:
        """Decide, perform at most one panel refresh (blocking), and return the
        number of seconds until the scheduler wants to run again (clamped to
        ``MIN_DELAY``..``MAX_DELAY``).  See docs/SCHEDULER.md for the order."""
        now = _aware(now)
        with self._tick_lock:
            if self._stop.is_set():
                return MIN_DELAY
            mono = self._monotonic()
            settings = self._store.get()
            with self._lock:
                self._observe_clock(now, mono)
                self._normalise(now, settings)
                plan = self._plan(now, mono, settings)
                pending = None
                if plan.action == "pending":
                    pending, self._pending = self._pending, None
                self._busy = plan.action != "wait"
            if plan.action == "wait":
                assert plan.until is not None
                delay = _clamp_delay((plan.until - now).total_seconds())
                log.debug("tick: %s; next look in %.1f s", plan.reason, delay)
                return delay
            log.debug("tick: %s", plan.reason)
            override: float | None = None
            try:
                if plan.action == "pending":
                    assert pending is not None
                    override = self._fire_pending(pending, now, mono, settings)
                elif plan.action == "keepalive":
                    override = self._keepalive(now, mono, settings)
                else:
                    assert plan.item is not None
                    override = self._show_item(
                        plan.item,
                        now,
                        mono,
                        settings,
                        origin=plan.origin,
                        keepalive_due=plan.keepalive_due,
                    )
            finally:
                with self._lock:
                    self._busy = False
            mono_after = self._monotonic()
            after = now + timedelta(seconds=max(0.0, mono_after - mono))
            with self._lock:
                follow = self._plan(after, mono_after, self._store.get())
            if follow.action == "wait" and follow.until is not None:
                delay = (follow.until - after).total_seconds()
                # a retry or backoff delay never hides an earlier event, such
                # as a keep-alive coming due inside a 60 s backoff
                return _clamp_delay(delay if override is None else min(override, delay))
            if override is not None and follow.action == "rotate":
                # the next slot after a failed render: the retry delay stands
                return _clamp_delay(override)
            return MIN_DELAY

    def _plan(self, now: datetime, mono: float, settings: Settings) -> _Plan:
        """The decision at wall time ``now`` / monotonic time ``mono``, without
        side effects except the shuffle pick cache.

        Order: (a) a due show-now; (b) a due keep-alive (after the floor; a
        due rotation refresh doubles as it); (c) paused; (d) quiet hours;
        backoff after a failed pass; (e)+(f)+(g) the rotation's next change,
        never before the floor.
        """
        floor_at = self._floor_at(settings, now, mono)
        wakes: list[datetime] = []

        # (a) show now (R2): bypasses the floor, not the show-now minimum
        if self._pending is not None:
            fires_at = self._pending_fires_at(settings, now, mono)
            if fires_at <= now:
                return _Plan("pending", f"show-now {self._pending.kind} is due")
            wakes.append(fires_at)

        quiet, quiet_until = quiet_window_active(settings.quiet_hours, now)
        backoff = self._backoff_until if self._backoff_until and now < self._backoff_until else None
        if settings.paused:
            blocked = "paused"
        elif quiet:
            blocked = "quiet hours"
        elif backoff is not None:
            blocked = "backing off after failed renders"
        else:
            blocked = None

        # (b) keep-alive (R3): also when paused or quiet, never before the floor
        keepalive_at = self._keepalive_deadline(settings)
        if now >= keepalive_at:
            if floor_at is not None and now < floor_at:
                return self._wait(now, [floor_at, *wakes], "keep-alive due; waiting for the floor")
            if blocked is None:
                rp = self._rotation_plan(now, mono, settings)
                if rp is not None and rp.due_at <= now:
                    reason = f"keep-alive due; rotation: {rp.reason}"
                    return _Plan("rotate", reason, item=rp.item, keepalive_due=True)
            if self._frame_available(settings):
                return _Plan("keepalive", "keep-alive due: re-show the last frame")
            if backoff is not None:
                return self._wait(now, [backoff, *wakes], "keep-alive due; render backoff")
            item = self._keepalive_item(settings)
            if item is not None:
                reason = "keep-alive due, no last frame: render the current slot"
                return _Plan("rotate", reason, item=item, origin="keepalive")
            # nothing to re-show and nothing to render: keep-alive cannot help
        else:
            wakes.append(keepalive_at if floor_at is None else max(keepalive_at, floor_at))

        # (c) paused, (d) quiet hours (R5), backoff (R6)
        if blocked == "paused":
            return self._wait(now, wakes, "paused")
        if blocked == "quiet hours":
            assert quiet_until is not None
            return self._wait(now, [quiet_until, *wakes], "quiet hours")
        if backoff is not None:
            return self._wait(now, [backoff, *wakes], blocked or "backoff")

        # (e) floor (R1), (f) rotation due, (g) wait for the next change
        rp = self._rotation_plan(now, mono, settings)
        if rp is None:
            return self._wait(now, wakes, "no enabled slots: keep-alive only")
        target = rp.due_at if floor_at is None else max(rp.due_at, floor_at)
        if target <= now:
            return _Plan("rotate", f"rotation: {rp.reason}", item=rp.item)
        why = "refresh floor" if floor_at is not None and floor_at > rp.due_at else rp.reason
        return self._wait(now, [target, *wakes], f"waiting: {why}")

    @staticmethod
    def _wait(now: datetime, wakes: Sequence[datetime], reason: str) -> _Plan:
        until = min(wakes) if wakes else now + timedelta(seconds=MAX_DELAY)
        return _Plan("wait", reason, until=until)

    # --- rotation model ----------------------------------------------------------------------

    def _enabled_indices(self, settings: Settings) -> list[int]:
        out = []
        for i, slot in enumerate(settings.rotation):
            if not slot.enabled:
                continue
            if slot.app_id not in self._apps:
                if slot.app_id not in self._warned_unknown:
                    self._warned_unknown.add(slot.app_id)
                    log.warning("rotation slot %d: unknown app %r is skipped", i, slot.app_id)
                continue
            out.append(i)
        return out

    def _next_enabled(
        self, settings: Settings, after: int, skip: set[int] | frozenset[int] = frozenset()
    ) -> int | None:
        """The first enabled slot after ``after`` (cyclic, may be ``after``
        itself), ignoring ``skip``; None if there is none."""
        candidates = [i for i in self._enabled_indices(settings) if i not in skip]
        if not candidates:
            return None
        return next((i for i in candidates if i > after), candidates[0])

    def _resolve_slot(self, settings: Settings) -> tuple[int, bool] | None:
        """``(slot index, fresh)`` for the current rotation position, following
        rotation edits; ``fresh`` means the slot has not shown its first frame.
        None when no slot is enabled.  Pure."""
        rot = settings.rotation
        enabled = self._enabled_indices(settings)
        if not enabled:
            return None
        st = self._state
        i, app_id = st.slot_index, st.slot_app_id
        not_started = st.slot_started_at is None
        if app_id is not None and 0 <= i < len(rot) and rot[i].app_id == app_id:
            if i in enabled:
                return i, not_started
            # the slot was disabled: move on to the next enabled one
            return next((j for j in enabled if j > i), enabled[0]), True
        if app_id is not None:
            # the rotation was edited: follow the slot by its app id
            same = [j for j in enabled if rot[j].app_id == app_id]
            if same:
                return min(same, key=lambda j: (j - i) % len(rot)), not_started
        start = min(max(i, 0), len(rot) - 1)
        return next((j for j in enabled if j >= start), enabled[0]), True

    def _db_read(self, what: str, read: Callable[[], _T], default: _T) -> _T:
        """``read()``, or ``default`` when the database read fails (a corrupt
        SD card, a row that does not parse).  The decision core and
        ``snapshot()`` must never raise because of storage: a failing read
        would otherwise stop every refresh, the keep-alive included.  The first
        failure of each ``what`` is logged with its traceback, its recovery
        once."""
        try:
            value = read()
        except _DB_READ_ERRORS as e:
            if what not in self._db_failing:
                log.error("could not read %s from the database", what, exc_info=True)
            self._db_failing[what] = f"{type(e).__name__}: {e}"
            return default
        if self._db_failing.pop(what, None) is not None:
            log.info("reading %s from the database works again", what)
        return value

    def _included_photos(self) -> list[Photo] | None:
        """The included photos, or None when the list cannot be read."""
        return self._db_read(
            "the photo list", lambda: self._db.list_photos(include_excluded=False), None
        )

    def _photo_list_error(self) -> str:
        why = self._db_failing.get("the photo list", "unknown error")
        return f"cannot read the photo list ({why})"

    def _photo_by_id(self, photo_id: str) -> Photo | None:
        """``db.get_photo``; an unreadable row is logged and reads as None."""
        return self._db_read("a photo", lambda: self._db.get_photo(photo_id), None)

    def _peek_photo(self, photos: Sequence[Photo], order: str, current: str | None) -> Photo | None:
        """The next photo, reserving a shuffle pick so the UI's "next" and the
        actual next photo agree."""
        if not photos:
            return None
        st = self._state
        if order != "sequential" and st.next_photo_id:
            cached = next((p for p in photos if p.id == st.next_photo_id), None)
            if cached is not None and not (cached.id == current and len(photos) > 1):
                return cached
        photo = pick_next_photo(photos, order, st.recent_photo_ids, self._rng, current=current)
        if order != "sequential" and photo is not None:
            st.next_photo_id = photo.id
        return photo

    def _entry_item(self, idx: int, settings: Settings) -> _Item:
        """What a new visit of slot ``idx`` shows first."""
        slot = settings.rotation[idx]
        if slot.app_id == PHOTOS_APP_ID:
            _, order = photo_options(slot.options)
            photos = self._included_photos()
            if photos is None:  # fails like a render, so the slot is skipped (R6)
                return _Item(idx, PHOTOS_APP_ID, None, "enter", error=self._photo_list_error())
            photo = self._peek_photo(photos, order, current=None)
            return _Item(idx, PHOTOS_APP_ID, photo, "enter")
        return _Item(idx, slot.app_id, None, "enter")

    def _rotation_plan(
        self, now: datetime, mono: float, settings: Settings
    ) -> _RotationPlan | None:
        """When the rotation wants its next change and what it shows then,
        ignoring pause, quiet hours, backoff and the floor (the caller applies
        those, except that a photo change which the floor would push past the
        slot's end is not planned).  None when no slot is enabled."""
        resolved = self._resolve_slot(settings)
        if resolved is None:
            return None
        idx, fresh = resolved
        st = self._state
        if fresh or st.slot_started_at is None:
            return _RotationPlan(now, self._entry_item(idx, settings), "slot has not shown yet")
        slot = settings.rotation[idx]
        slot_end = st.slot_started_at + timedelta(minutes=slot.duration_minutes)
        if slot.app_id == PHOTOS_APP_ID:
            dwell, order = photo_options(slot.options)
            # None (unreadable): no photo change; the photo on the panel stays
            # until the slot ends
            photos = self._included_photos()
            change_at = (st.photo_started_at or st.slot_started_at) + dwell
            if photos is not None and not photos:
                # the "no photos" card lasts one dwell, then the rotation moves on
                nxt = self._next_enabled(settings, idx)
                assert nxt is not None
                end = min(slot_end, change_at)
                return _RotationPlan(end, self._entry_item(nxt, settings), "photos: none to show")
            if photos is not None and any(p.id != st.photo_id for p in photos):
                floor_at = self._floor_at(settings, now, mono)
                earliest = change_at if floor_at is None else max(change_at, floor_at)
                # a photo change that would push the slot past its end (the
                # next refresh cannot come before the floor) is skipped
                if earliest + timedelta(seconds=self._floor_seconds(settings)) <= slot_end:
                    photo = self._peek_photo(photos, order, current=st.photo_id)
                    item = _Item(idx, PHOTOS_APP_ID, photo, "photo")
                    return _RotationPlan(change_at, item, "photos: dwell elapsed")
        nxt = self._next_enabled(settings, idx)
        assert nxt is not None
        return _RotationPlan(slot_end, self._entry_item(nxt, settings), "slot duration elapsed")

    def _keepalive_item(self, settings: Settings) -> _Item | None:
        """What a keep-alive renders when no last frame exists: the current
        slot's content, without advancing the rotation.

        In a photos slot whose photo is gone (deleted, or the slot showed the
        "no photos" card and photos have been added since), that is the
        slot's next photo, shown as a photo change inside the slot, rather
        than a "no photos" card while photos exist."""
        resolved = self._resolve_slot(settings)
        if resolved is None:
            return None
        idx, fresh = resolved
        st = self._state
        if fresh or st.slot_started_at is None:
            return self._entry_item(idx, settings)
        slot = settings.rotation[idx]
        if slot.app_id == PHOTOS_APP_ID:
            photo = self._photo_by_id(st.photo_id) if st.photo_id else None
            if photo is not None:
                return _Item(idx, PHOTOS_APP_ID, photo, "keep")
            photos = self._included_photos()
            if photos is None:  # fails like a render, so the keep-alive walks on (R6)
                return _Item(idx, PHOTOS_APP_ID, None, "keep", error=self._photo_list_error())
            _, order = photo_options(slot.options)
            nxt = self._peek_photo(photos, order, current=None)
            return _Item(idx, PHOTOS_APP_ID, nxt, "photo" if nxt is not None else "keep")
        return _Item(idx, slot.app_id, None, "keep")

    def _find_slot(self, settings: Settings, app_id: str, current: int | None) -> int | None:
        """The enabled slot of ``app_id`` nearest at/after ``current`` (cyclic)."""
        matches = [
            i for i in self._enabled_indices(settings) if settings.rotation[i].app_id == app_id
        ]
        if not matches:
            return None
        if current is None:
            return matches[0]
        n = len(settings.rotation)
        return min(matches, key=lambda i: (i - current) % n)

    def _pending_item(
        self, req: _Pending, now: datetime, mono: float, settings: Settings
    ) -> _Item | None:
        """Translate a show-now request into an item.  Showing something that
        has a slot jumps the rotation there; anything else is shown without
        touching the rotation timing."""
        if req.kind == "next":
            rp = self._rotation_plan(now, mono, settings)
            return rp.item if rp is not None else None
        resolved = self._resolve_slot(settings)
        cur = resolved[0] if resolved is not None else None
        cur_started = (
            resolved is not None and not resolved[1] and self._state.slot_started_at is not None
        )
        app_id = PHOTOS_APP_ID if req.kind == "photo" else str(req.id)
        target = self._find_slot(settings, app_id, cur)
        photo: Photo | None = None
        error: str | None = None
        if req.kind == "photo":
            photo = self._photo_by_id(str(req.id))
            if photo is None:
                return None
        elif app_id == PHOTOS_APP_ID:
            order = (
                photo_options(settings.rotation[target].options)[1]
                if target is not None
                else DEFAULT_PHOTO_ORDER
            )
            photos = self._included_photos()
            if photos is None:
                error = self._photo_list_error()
            else:
                photo = self._peek_photo(photos, order, current=self._state.photo_id)
        variant = req.variant if req.kind == "app" else None
        if target is None:
            return _Item(None, app_id, photo, "keep", error, variant)
        if target == cur and cur_started:
            mode = "photo" if photo is not None else "keep"
            return _Item(target, app_id, photo, mode, error, variant)
        return _Item(target, app_id, photo, "enter", error, variant)

    # --- actions ---------------------------------------------------------------------------

    def _fire_pending(
        self, req: _Pending, now: datetime, mono: float, settings: Settings
    ) -> float | None:
        with self._lock:
            item = self._pending_item(req, now, mono, settings)
        if item is None:
            log.warning("show-now %s %s: nothing to show; request dropped", req.kind, req.id)
            return None
        log.info("show-now: %s %s", req.kind, req.label if req.kind != "next" else "")
        return self._show_item(item, now, mono, settings, origin="show_now")

    def _keepalive(self, now: datetime, mono: float, settings: Settings) -> float | None:
        frame = self._load_last_frame(settings)
        if frame is None:  # vanished since the plan was made
            with self._lock:
                item = self._keepalive_item(settings)
            if item is None:
                return None
            return self._show_item(item, now, mono, settings, origin="keepalive")
        with self._lock:
            showing = self._state.showing or {}
        source = {
            "type": "keepalive",
            "id": showing.get("id"),
            "label": showing.get("label") or "last frame",
        }
        self._refresh(frame, source, now, mono, settings, detail="keep-alive")
        return None

    def _source_for(self, item: _Item, settings: Settings) -> dict[str, Any]:
        if item.photo is not None:
            return {"type": "photo", "id": item.photo.id, "label": item.photo.original_name}
        app = self._apps.get(item.app_id)
        source = {"type": "app", "id": item.app_id, "label": app.name if app else item.app_id}
        if item.variant is not None:
            source["variant"] = item.variant  # a forced layout (the test card)
        return source

    def _already_showing(self, source: Mapping[str, Any]) -> bool:
        """True when ``source`` is static content (a photo, or the photos
        slot's "no photos" card) that is on the panel already.  Never while
        the panel is stale (after a failed attempt its content is unknown:
        white, say, if ``clear()`` worked and ``show()`` did not), so the next
        rotation change repaints it."""
        st = self._state
        showing = st.showing
        static = source["type"] == "photo" or source["id"] == PHOTOS_APP_ID
        return (
            static
            and not st.panel_stale
            and showing is not None
            and (showing.get("type"), showing.get("id")) == (source["type"], source["id"])
        )

    def _show_item(
        self,
        item: _Item,
        now: datetime,
        mono: float,
        settings: Settings,
        *,
        origin: str,
        keepalive_due: bool = False,
    ) -> float | None:
        """Render ``item`` and refresh the panel with it.  Returns a delay
        override for ``tick`` (after a render failure) or None.

        ``keepalive_due``: the keep-alive is due as well; if the item turns out
        to be on the panel already (no refresh), the last frame is re-shown so
        the keep-alive is not put off to a later tick."""
        source = self._source_for(item, settings)
        if origin == "rotation" and item.error is None:
            with self._lock:
                repeat = self._already_showing(source)
                if repeat:
                    # the same photo (or "no photos" card) is already on the
                    # panel: restart the timers instead of a pointless refresh
                    self._mark_shown(item, now, source, settings)
                    self._save()
                    log.debug("rotation: %s %s already showing", source["type"], source["id"])
            if repeat:
                return self._keepalive(now, mono, settings) if keepalive_due else None
        try:
            if item.error is not None:
                raise _Unavailable(item.error)
            frame = self._render(item, now, settings)
        except _Stopping as e:
            log.info("%s", e)
            return None
        except BaseException as e:  # noqa: BLE001 - any app failure is skipped (R6)
            # BaseException too: a render that raises SystemExit (a library
            # calling sys.exit(); the render executor hands it back through
            # future.result()) would otherwise end this thread, silently
            if _is_ctrl_c(e):
                raise
            what = f"{item.app_id}{f' photo {item.photo.id}' if item.photo is not None else ''}"
            if isinstance(e, (_Unavailable, RenderTimeout)):
                log.error("render of %s failed: %s; skipping", what, e)
            else:
                log.exception("render of %s failed; skipping", what)
            with self._lock:
                self._record_error("render", item.app_id, e, now)
                if origin == "show_now":
                    self._save()
                    return RETRY_DELAY
                delay = self._skip_after_failure(item, now, settings)
                self._save()
            return delay
        if origin == "rotation":
            latest = self._store.get()
            later = now + timedelta(seconds=max(0.0, self._monotonic() - mono))
            if latest.paused or quiet_window_active(latest.quiet_hours, later)[0]:
                log.info("rotation paused or quiet hours began while rendering; frame not shown")
                return None
        detail = source["label"]
        if item.photo is not None:
            # a stored recipe that is not an object (corrupt row, a write that
            # bypassed Database.set_photo_recipe) must not raise here: anything
            # raised between the render and the refresh would escape tick()
            # and leave the rotation stuck on this photo
            recipe = item.photo.recipe if isinstance(item.photo.recipe, Mapping) else None
            detail = str((recipe or settings.default_recipe).get("method", "default"))
        finished = self._refresh(frame, source, now, mono, settings, detail=detail)
        if finished is not None:
            with self._lock:
                self._mark_shown(item, finished, source, settings)
                self._save()
        return None

    def _render(self, item: _Item, now: datetime, settings: Settings) -> np.ndarray:
        app = self._apps[item.app_id]
        ctx = build_render_context(
            app,
            settings=settings,
            db=self._db,
            paths=self._paths,
            now=now,
            fonts=self._fonts,
            online=self._online,
            item_id=item.photo.id if item.photo is not None else None,
            variant=item.variant,
        )

        def job() -> np.ndarray:
            result = render_to_frame(
                app, ctx, recipe=settings.default_recipe, render_palette=settings.render_palette()
            )
            # render_to_frame returns the dither engine's output unchecked: a
            # bad frame must fail here, as a render failure (the slot is
            # skipped), not later in display.show() as a panel failure that
            # retries the same item every floor, after a clear() each time
            try:
                return validate_frame(result)
            except ValueError as e:
                raise ValueError(f"{app.id} rendered an invalid frame: {e}") from None

        if self._executor is None:
            return job()
        stuck = self._stuck_render
        if stuck is not None:
            if not stuck.done():
                # submitting now would only queue behind it, time out too and
                # pile up: fail at once until the render thread is free again
                raise RenderTimeout(
                    f"an earlier render ({self._stuck_label}) timed out and is still running"
                )
            self._stuck_render = None
            log.info("the timed-out %s render has finished; rendering again", self._stuck_label)
        future = self._executor.submit(job)
        # wait in short slices so stop() is not held up by a slow render
        deadline = time.monotonic() + self._render_timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                return future.result(timeout=max(0.0, min(remaining, STOP_POLL_SECONDS)))
            except FutureTimeout:
                pass
            stopping = self._stop.is_set()
            if stopping or remaining <= STOP_POLL_SECONDS:
                break
        # a job still queued (behind a preview, say) is dropped; one that is
        # running cannot be stopped, so remember it
        if not future.cancel():
            self._stuck_render = future
            self._stuck_label = item.app_id
        if stopping:
            raise _Stopping(f"stopping: the {item.app_id} render was abandoned")
        raise RenderTimeout(f"{item.app_id} did not render within {self._render_timeout:g} s")

    def _refresh(
        self,
        frame: np.ndarray,
        source: dict[str, Any],
        now: datetime,
        mono: float,
        settings: Settings,
        *,
        detail: str,
    ) -> datetime | None:
        """One panel refresh: init -> [clear] -> show -> sleep (always).

        ``now``/``mono`` are the tick's clock readings; the times recorded are
        ``now`` plus the monotonic time since.  The attempt is recorded just
        before the panel is touched (after the render, so a crash or power cut
        mid-refresh still counts for the floor from the right moment) and again
        when it ends.  From that first write until a refresh succeeds the
        panel is marked stale (``panel_stale``): after a failed or interrupted
        attempt nobody knows what it shows.  A failed attempt restores the
        last good frame on disk (a driver may have written something else
        there, e.g. MockDisplay's white ``clear()``), or, if there was none,
        removes what the driver wrote, so a restart keeps the right image
        alive.  Returns the time the refresh finished, or None if it failed
        (or the scheduler is stopping, in which case the panel is not touched
        at all).
        """
        self._refreshing = True  # before the stop check: see ``refreshing``
        try:
            if self._stop.is_set():
                log.info("stopping: %s not shown", source.get("type"))
                return None
            return self._drive_panel(frame, source, now, mono, settings, detail=detail)
        finally:
            self._refreshing = False

    def _drive_panel(
        self,
        frame: np.ndarray,
        source: dict[str, Any],
        now: datetime,
        mono: float,
        settings: Settings,
        *,
        detail: str,
    ) -> datetime | None:
        """The body of ``_refresh`` once the stop flag has been checked."""
        previous = self._load_last_frame(settings)
        frame_file = self._frame_file_probe()
        started = self._monotonic()
        with self._lock:
            self._state.last_attempt_at = now + timedelta(seconds=max(0.0, started - mono))
            self._attempt_mono = started
            self._live.add(_ATTEMPT_ANCHOR)
            stale_before = self._state.panel_stale
            self._state.panel_stale = True  # until this attempt is known to have worked
            self._save()
        error: BaseException | None = None
        try:
            self._display.init()
            if settings.display.clear_before_refresh:
                self._display.clear()
            self._display.show(frame)
        except (DisplayError, OSError) as e:
            error = e
            log.error("panel refresh failed: %s: %s", type(e).__name__, e, exc_info=True)
        except Exception as e:  # noqa: BLE001 - a driver bug must not kill the thread
            error = e
            log.exception("unexpected error while refreshing the panel")
        finally:
            try:
                self._display.sleep()
            except Exception:  # sleep() never raises by contract; belt and braces
                log.exception("display.sleep() failed")
        ended = self._monotonic()
        took = ended - started
        finished = now + timedelta(seconds=max(0.0, ended - mono))
        if error is None:
            rotation = self._persist_frame(frame, settings)
        elif previous is not None:
            rotation = self._persist_frame(previous, settings)
        else:
            rotation = None
            if self._frame_file_probe() != frame_file:
                # nothing good to restore: drop what the driver wrote during
                # the failed attempt (MockDisplay's white clear()), so neither
                # a keep-alive nor a restart re-shows it as "the last frame"
                self._discard_frame_files()
        with self._lock:
            st = self._state
            st.last_attempt_at = finished
            self._attempt_mono = ended
            if error is None:
                st.last_refresh_at = finished
                self._live.add("last_refresh_at")
                st.last_source = dict(source)
                # a keep-alive re-shows the last good frame, which may be the
                # outdated render ``invalidate()`` asked to replace
                st.panel_stale = stale_before if source.get("type") == "keepalive" else False
                self._last_frame = np.array(frame, dtype=np.uint8, copy=True)
                self._bad_frame_file = None
            else:
                self._record_error("display", _app_of(source), error, finished)
            if rotation is not None:
                st.frame_rotation = rotation
            self._save(mirror=error is None)
        ident = str(source.get("id") or "")
        if source.get("type") == "photo" or len(ident) > 16:
            ident = ident[:8] + "..."
        if error is None:
            log.info("refresh: %s %s (%s) took %.1f s", source.get("type"), ident, detail, took)
        else:
            log.info(
                "refresh: %s %s (%s) FAILED after %.1f s", source.get("type"), ident, detail, took
            )
        return finished if error is None else None

    # --- state transitions (called with the lock held) ----------------------------------------

    def _mark_shown(
        self, item: _Item, at: datetime, source: dict[str, Any], settings: Settings
    ) -> None:
        st = self._state
        if item.mode == "enter" and item.slot_index is not None:
            st.slot_index = item.slot_index
            st.slot_app_id = item.app_id
            st.slot_started_at = at
            if item.app_id == PHOTOS_APP_ID:
                st.photo_id = item.photo.id if item.photo is not None else None
                st.photo_started_at = at
            else:
                st.photo_id = None
                st.photo_started_at = None
        elif item.mode == "photo":
            st.photo_id = item.photo.id if item.photo is not None else None
            st.photo_started_at = at
        if item.photo is not None:
            self._remember_photo(item.photo.id)
        st.showing = {k: source[k] for k in ("type", "id", "label")}
        self._failed_slots.clear()

    def _remember_photo(self, photo_id: str) -> None:
        st = self._state
        st.recent_photo_ids.append(photo_id)
        del st.recent_photo_ids[:-RECENT_PHOTOS_KEPT]
        st.next_photo_id = None

    def _skip_after_failure(self, item: _Item, now: datetime, settings: Settings) -> float:
        """R6: skip the failed slot for this pass and point the rotation at the
        next enabled one; back off when every enabled slot failed.

        This also runs for a keep-alive that had to render (there was no last
        frame) and failed, even while paused or in quiet hours: it is the one
        case in which a keep-alive moves the rotation, so that it walks on to a
        slot that does render instead of retrying a broken one until the
        panel goes past its keep-alive."""
        st = self._state
        if item.photo is not None:
            self._remember_photo(item.photo.id)  # the walk moves past a broken photo
        if item.slot_index is None:
            return RETRY_DELAY
        self._failed_slots.add(item.slot_index)
        nxt = self._next_enabled(settings, item.slot_index, skip=self._failed_slots)
        if nxt is None:
            self._failed_slots.clear()
            self._backoff_until = now + timedelta(seconds=BACKOFF_SECONDS)
            nxt = self._next_enabled(settings, item.slot_index)
            log.error(
                "every enabled slot failed to render; the panel keeps its frame, "
                "retrying in %.0f s",
                BACKOFF_SECONDS,
            )
            delay = BACKOFF_SECONDS
        else:
            delay = RETRY_DELAY
        if nxt is not None:
            st.slot_index = nxt
            st.slot_app_id = settings.rotation[nxt].app_id
            st.slot_started_at = None
        return delay

    def _record_error(
        self, kind: str, app_id: str | None, error: BaseException, at: datetime
    ) -> None:
        message = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
        self._state.last_error = {"at": iso(at), "kind": kind, "app_id": app_id, "message": message}

    def _normalise(self, now: datetime, settings: Settings) -> None:
        """Clamp impossible future anchors and fold rotation edits into the state."""
        st = self._state
        changed = self._clamp_future(now)
        resolved = self._resolve_slot(settings)
        if resolved is not None:
            idx, fresh = resolved
            app_id = settings.rotation[idx].app_id
            if (idx, app_id) != (st.slot_index, st.slot_app_id) or (
                fresh and st.slot_started_at is not None
            ):
                if st.slot_app_id is not None and (idx, app_id) != (st.slot_index, st.slot_app_id):
                    log.info(
                        "rotation edited: slot %d (%s) is now slot %d (%s)%s",
                        st.slot_index,
                        st.slot_app_id,
                        idx,
                        app_id,
                        ", starting it fresh" if fresh else "",
                    )
                st.slot_index, st.slot_app_id = idx, app_id
                if fresh:
                    st.slot_started_at = None
                changed = True
        if changed:
            self._save()

    def _clamp_future(self, now: datetime) -> bool:
        st = self._state
        limit = now + timedelta(seconds=CLOCK_JUMP_SECONDS)
        changed = False
        for name in _TIME_FIELDS:
            t = getattr(st, name)
            if t is not None and t > limit:
                log.warning("%s %s is in the future (clock behind?); using now", name, iso(t))
                setattr(st, name, now)
                # "now" is a stand-in, not the time of an event on this clock
                self._live.discard(name)
                changed = True
        return changed

    def _observe_clock(self, now: datetime, mono: float) -> None:
        """Shift the anchors by a wall-clock jump the monotonic clock did not see.

        An anchor this process set (in ``self._live``, or a rotation/backoff
        anchor) was taken on the clock as it was before the jump and moves
        with it.  A keep-alive or attempt anchor loaded from disk (or clamped
        at start-up) was written on a clock whose offset to this one is
        unknown: after a power cut the Pi boots with the clock it saved before
        the cut, hours behind, so the first NTP step must not carry an anchor
        written in true time along with it.  Those move only in their safe
        direction: keep-alive anchors only earlier (never a later keep-alive),
        the attempt anchor only later (never a shorter floor).
        """
        ref = self._clock_ref
        self._clock_ref = (now, mono)
        if ref is None:
            return
        # instants, not wall clocks: two datetimes sharing one DST-aware tzinfo
        # subtract on the wall clock, which would fake a jump at a DST change
        utc = timezone.utc
        skew = (now.astimezone(utc) - ref[0].astimezone(utc)).total_seconds() - (mono - ref[1])
        if abs(skew) <= CLOCK_JUMP_SECONDS:
            return
        log.warning("wall clock jumped by %+.0f s; shifting the scheduler's timers", skew)
        shift = timedelta(seconds=skew)
        st = self._state
        for name in _TIME_FIELDS:
            t = getattr(st, name)
            if t is None:
                continue
            if name not in self._live:
                if skew > 0 and name in _KEEPALIVE_ANCHORS:
                    continue
                if skew < 0 and name == _ATTEMPT_ANCHOR:
                    continue
            setattr(st, name, t + shift)
        if self._backoff_until is not None:
            self._backoff_until += shift
        self._save()

    # --- timing helpers -------------------------------------------------------------------

    @staticmethod
    def _floor_seconds(settings: Settings) -> int:
        return max(int(settings.panel.refresh_floor_seconds), MIN_REFRESH_FLOOR_SECONDS)

    def _show_now_seconds(self, settings: Settings) -> int:
        floor = self._floor_seconds(settings)
        return min(max(int(settings.panel.show_now_min_seconds), MIN_SHOW_NOW_SECONDS), floor)

    def _attempt_anchor(self, now: datetime, mono: float) -> datetime | None:
        """The end of the last refresh attempt, on the wall clock of ``now``,
        for the floor (R1) and the show-now minimum (R2).

        That is the recorded ``last_attempt_at``, moved later when the
        monotonic clock says less time has passed since this process's last
        attempt than the wall clock does (a wall-clock step forward of any
        size, including the ones under ``CLOCK_JUMP_SECONDS`` that are not
        shifted), so both clocks must agree that the floor has passed.
        """
        t = self._state.last_attempt_at
        if self._attempt_mono is not None:
            seen = now - timedelta(seconds=max(0.0, mono - self._attempt_mono))
            if t is None or seen > t:
                t = seen
        return t

    def _floor_at(self, settings: Settings, now: datetime, mono: float) -> datetime | None:
        t = self._attempt_anchor(now, mono)
        return None if t is None else t + timedelta(seconds=self._floor_seconds(settings))

    def _pending_fires_at(self, settings: Settings, now: datetime, mono: float) -> datetime:
        """When a queued show-now may refresh: now, or ``show_now_min_seconds``
        after the last attempt if that is later.  (Not anchored to the request
        time, which is taken outside a tick and so cannot be corrected for a
        clock jump; it never mattered anyway, since a request is never due
        before it was made.)"""
        last = self._attempt_anchor(now, mono)
        if last is None:
            return now
        return max(now, last + timedelta(seconds=self._show_now_seconds(settings)))

    def _keepalive_deadline(self, settings: Settings) -> datetime:
        """When the keep-alive is due: ``keepalive_hours`` after the last
        successful refresh; immediately if none is recorded but a last frame
        exists; else ``keepalive_hours`` after this state was created."""
        keep = timedelta(hours=settings.panel.keepalive_hours)
        st = self._state
        if st.last_refresh_at is not None:
            deadline = st.last_refresh_at + keep
            if (
                st.panel_stale
                and st.last_attempt_at is not None
                and st.last_attempt_at > st.last_refresh_at
            ):
                # the last attempt failed after the last success (panel_stale is
                # set before every attempt and cleared only by a success, so a
                # clock jump that moves a loaded attempt anchor later cannot
                # fake this): with clear_before_refresh the panel may be sitting
                # white, and while paused or in quiet hours no rotation change
                # would repair it for up to keepalive_hours.  Re-show the last
                # good frame as soon as the floor allows instead (R1 still
                # holds: one attempt per floor; a broken panel retries at this
                # slower pace).
                repair = timedelta(seconds=max(self._floor_seconds(settings), REPAIR_RETRY_SECONDS))
                deadline = min(deadline, st.last_attempt_at + repair)
            return deadline
        if self._frame_available(settings):
            return _EPOCH
        return (st.created_at or _EPOCH) + keep

    def _next_rotation_at(
        self, now: datetime, mono: float, settings: Settings, plan: _RotationPlan | None
    ) -> datetime | None:
        """Best estimate of the next rotation refresh (None when paused or empty)."""
        if settings.paused or plan is None:
            return None
        t = max(plan.due_at, now)
        floor_at = self._floor_at(settings, now, mono)
        if floor_at is not None:
            t = max(t, floor_at)
        if self._backoff_until is not None:
            t = max(t, self._backoff_until)
        # quiet hours are local wall-clock times; anchors read back from disk
        # are UTC, so look at ``t`` on the clock of ``now``
        active, until = quiet_window_active(settings.quiet_hours, t.astimezone(now.tzinfo))
        if active and until is not None:
            t = max(t, until)
        return t

    # --- reporting ---------------------------------------------------------------------------

    def _label(self, app_id: str, photo: Photo | None) -> str:
        if photo is not None:
            return photo.original_name
        app = self._apps.get(app_id)
        return app.name if app is not None else app_id

    def _item_info(self, item: _Item) -> dict[str, Any]:
        return {
            "app_id": item.app_id,
            "label": self._label(item.app_id, item.photo),
            "photo_id": item.photo.id if item.photo is not None else None,
        }

    def _current_info(self, settings: Settings) -> dict[str, Any] | None:
        resolved = self._resolve_slot(settings)
        st = self._state
        if resolved is None or resolved[1] or st.slot_started_at is None:
            return None
        idx = resolved[0]
        slot = settings.rotation[idx]
        photo = None
        if slot.app_id == PHOTOS_APP_ID and st.photo_id:
            photo = self._photo_by_id(st.photo_id)
        return {
            "app_id": slot.app_id,
            "label": self._label(slot.app_id, photo),
            "photo_id": st.photo_id if slot.app_id == PHOTOS_APP_ID else None,
            "started_at": iso(st.slot_started_at),
            "ends_at": iso(st.slot_started_at + timedelta(minutes=slot.duration_minutes)),
        }

    def _pending_info(
        self, settings: Settings, now: datetime, mono: float
    ) -> dict[str, Any] | None:
        """The pending request as reported (``snapshot()``, ``show_now``).

        While ``tick`` is rendering or refreshing, the attempt in flight ends
        later than ``now``, so ``fires_at`` is at least ``show_now_min_seconds``
        from now (the real time follows once it has ended) instead of a time
        measured from the previous attempt, which may already have passed.
        """
        req = self._pending
        if req is None:
            return None
        label = req.label
        if req.kind == "next":
            plan = self._rotation_plan(now, mono, settings)
            label = self._label(plan.item.app_id, plan.item.photo) if plan else "Next"
        fires_at = self._pending_fires_at(settings, now, mono)
        if self._busy:
            fires_at = max(fires_at, now + timedelta(seconds=self._show_now_seconds(settings)))
        return {"kind": req.kind, "id": req.id, "label": label, "fires_at": iso(fires_at)}

    def _queue(self, req: _Pending) -> dict[str, Any]:
        settings = self._store.get()
        mono = self._monotonic()
        with self._lock:
            replaced = self._pending
            self._pending = req
            info = self._pending_info(settings, req.requested_at, mono)
        assert info is not None
        log.info(
            "show-now queued: %s %s, fires at %s%s",
            req.kind,
            req.id or "",
            info["fires_at"],
            f" (replaces {replaced.kind} {replaced.id or ''})" if replaced else "",
        )
        self.wake()
        return info

    # --- frames and persistence --------------------------------------------------------------

    def _frame_available(self, settings: Settings) -> bool:
        return self._load_last_frame(settings) is not None

    def _load_last_frame(self, settings: Settings) -> np.ndarray | None:
        """The frame on the panel: from memory, else ``last_frame.bin``."""
        with self._lock:
            if self._last_frame is not None:
                return self._last_frame
            path = self._paths.last_frame_bin
            try:
                stat = path.stat()
            except OSError:
                return None
            probe = (stat.st_mtime_ns, stat.st_size)
            if probe == self._bad_frame_file:
                return None
            rotation = self._state.frame_rotation
            if rotation not in VALID_ROTATIONS:
                rotation = settings.display.rotation
            try:
                frame = unpack_frame(path.read_bytes(), rotation)
            except (OSError, ValueError) as e:
                log.warning("ignoring unreadable %s: %s", path.name, e)
                self._bad_frame_file = probe
                return None
            self._last_frame = frame
            return frame

    def _persist_frame(self, frame: np.ndarray, settings: Settings) -> int | None:
        """Write ``last_frame.bin`` (packed with the current rotation) and
        ``last_frame.png`` (measured colours) atomically; returns the rotation."""
        rotation = settings.display.rotation
        try:
            self._paths.root.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(self._paths.last_frame_bin, pack_frame(frame, rotation))
            buf = io.BytesIO()
            indices_to_image(frame, settings.measured_palette()).save(
                buf, format="PNG", compress_level=6
            )
            atomic_write_bytes(self._paths.last_frame_png, buf.getvalue())
        except (OSError, ValueError):
            log.exception("could not write the last frame files")
            return None
        return rotation

    def _frame_file_probe(self) -> tuple[int, int, int] | None:
        """``(inode, mtime_ns, size)`` of ``last_frame.bin``, None if absent;
        tells whether a driver rewrote it (atomic writes make a new file)."""
        try:
            stat = self._paths.last_frame_bin.stat()
        except OSError:
            return None
        return stat.st_ino, stat.st_mtime_ns, stat.st_size

    def _discard_frame_files(self) -> None:
        """Remove ``last_frame.bin`` and ``last_frame.png`` (missing is fine)."""
        for path in (self._paths.last_frame_bin, self._paths.last_frame_png):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                log.warning("could not remove %s", path.name, exc_info=True)
                continue
            log.info(
                "removed %s, written during a failed refresh with no frame to restore", path.name
            )

    def _read_state(self, key: str) -> Any:
        """``db.get_state(key)``, or None when the row cannot be read: text
        that is not JSON (a torn write, SD-card bit rot, a hand edit) or a
        failing database must not stop the frame from booting."""
        return self._db_read(f"the {key!r} state", lambda: self._db.get_state(key), None)

    def _load_state(self, now: datetime) -> SchedulerState:
        """The persisted state, tolerant of anything: unreadable or invalid
        data gives defaults, field by field.

        The floor and the show-now minimum must survive a lost attempt
        anchor (R1, R2): a missing ``last_attempt_at`` (or one older than the
        last refresh) is the last refresh, since an attempt ended at least
        then.  If neither is known because the state row itself was lost,
        and a frame on disk says the panel has been driven, the start-up
        time stands in, so the first refresh waits one floor.
        """
        raw = self._db_read(
            f"the {STATE_KEY!r} state", lambda: self._db.get_state(STATE_KEY), _UNREADABLE
        )
        lost = raw is _UNREADABLE or (raw is not None and not isinstance(raw, dict))
        if raw is _UNREADABLE:
            raw = None
        elif lost:
            log.warning("ignoring malformed scheduler state %r", raw)
        st = SchedulerState.from_json(raw)
        if st.last_refresh_at is None:
            st.last_refresh_at = _parse_time(self._read_state(LAST_REFRESH_KEY))
        if st.last_refresh_at is not None and (
            st.last_attempt_at is None or st.last_attempt_at < st.last_refresh_at
        ):
            st.last_attempt_at = st.last_refresh_at
        if st.last_attempt_at is None and lost and self._frame_file_probe() is not None:
            log.warning("last refresh attempt unknown; the first refresh waits one floor")
            st.last_attempt_at = now
        if st.last_source is None:
            src = self._read_state(LAST_SOURCE_KEY)
            if isinstance(src, dict):
                st.last_source = dict(src)
        if st.created_at is None:
            st.created_at = now
            self._live.add("created_at")
        settings = self._store.get()
        n = len(settings.rotation)
        st.slot_index = min(st.slot_index, n - 1) if n else 0
        for name in ("photo_id", "next_photo_id"):
            pid = getattr(st, name)
            # an unreadable photo row is kept (it may be readable later)
            if pid is not None and not self._db_read(
                "a photo", lambda pid=pid: self._db.get_photo(pid) is not None, True
            ):
                setattr(st, name, None)
                if name == "photo_id":
                    st.photo_started_at = None
        self._state = st
        self._clamp_future(now)
        self._save()
        log.info(
            "scheduler state: slot %d (%s), last refresh %s",
            st.slot_index,
            st.slot_app_id or "-",
            iso(st.last_refresh_at) or "never",
        )
        return st

    def _save(self, *, mirror: bool = False) -> None:
        """Persist the state (and, after a successful refresh, the two mirror
        keys ``/api/status`` reads).  A storage error is logged, never raised:
        the in-memory state stays authoritative."""
        st = self._state
        try:
            self._db.set_state(STATE_KEY, st.to_json())
            if mirror:
                self._db.set_state(LAST_REFRESH_KEY, iso(st.last_refresh_at))
                self._db.set_state(LAST_SOURCE_KEY, st.last_source)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            log.exception("could not persist the scheduler state")
