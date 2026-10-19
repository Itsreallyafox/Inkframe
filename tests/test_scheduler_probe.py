"""Adversarial probes against the scheduler's panel-care rules (review rounds 1
and 2), kept as regression tests.

Every test here FAILED against the code as reviewed; each one names the rule
it broke and the scenario, and now passes with its assertions unchanged (the
fixes are described in docs/SCHEDULER.md).  They use the same rig as
``test_scheduler.py`` (tests/conftest.py) plus two clocks of their own:

* ``JumpClock``: wall clock and monotonic clock that can disagree, to model
  NTP stepping the wall clock on a Pi Zero with no RTC;
* ``EasternClock``: a real instant shown in a DST-aware ``America/New_York``
  zone (``zoneinfo`` when tzdata is installed, else an equivalent PEP 495
  fold-aware ``tzinfo``), so the scheduler sees 02:00 skipped in March and
  01:00 repeated in November exactly as ``zoneinfo`` would hand them over.

The tests under "review round 2" failed against the code as reviewed in
round 2; each docstring names the rule, the scenario and what was observed
then.  They pass now, with their assertions unchanged.

The tests under "review round 3" at the end of the file FAIL against the code
as reviewed in round 3 (each docstring says what was observed); the fixer
makes them pass without weakening their assertions.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from inkframe.config import QuietHours, SettingsStore
from inkframe.db import Database
from inkframe.display.palette import WHITE
from inkframe.paths import DataPaths
from inkframe.scheduler import CRASH_DELAY, STATE_KEY, Scheduler, iso, quiet_window_active
from tests.conftest import START, FakeApp, FakeClock, RecordingDisplay, at, fake_apps, slot

# --- clocks ---------------------------------------------------------------------------


class JumpClock:
    """Wall clock + monotonic clock; ``jump`` moves only the wall clock."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.mono = 5000.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        """Real time passes: both clocks move."""
        self.now += timedelta(seconds=seconds)
        self.mono += seconds

    def jump(self, seconds: float) -> None:
        """NTP steps the wall clock; no real time passes."""
        self.now += timedelta(seconds=seconds)


_HOUR = timedelta(hours=1)
_ZERO = timedelta(0)


def _first_sunday_on_or_after(dt: datetime) -> datetime:
    return dt + timedelta(days=(6 - dt.weekday()) % 7)


class _UsEastern(tzinfo):
    """America/New_York (US rules since 2007) with PEP 495 ``fold`` support;
    the Python docs' ``USTimeZone`` example, used when tzdata is missing."""

    _std = timedelta(hours=-5)

    def _range(self, year: int) -> tuple[datetime, datetime]:
        start = _first_sunday_on_or_after(datetime(year, 3, 8, 2))
        end = _first_sunday_on_or_after(datetime(year, 11, 1, 2))
        return start, end

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return self._std + self.dst(dt)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None or dt.tzinfo is None:
            return _ZERO
        start, end = self._range(dt.year)
        dt = dt.replace(tzinfo=None)
        if start + _HOUR <= dt < end - _HOUR:
            return _HOUR
        if end - _HOUR <= dt < end:  # the repeated hour
            return _ZERO if dt.fold else _HOUR
        if start <= dt < start + _HOUR:  # the missing hour
            return _HOUR if dt.fold else _ZERO
        return _ZERO

    def tzname(self, dt: datetime | None) -> str:
        return "EDT" if self.dst(dt) else "EST"

    def fromutc(self, dt: datetime) -> datetime:
        start, end = self._range(dt.year)
        start = start.replace(tzinfo=self)
        end = end.replace(tzinfo=self)
        std_time = dt + self._std
        dst_time = std_time + _HOUR
        if end <= dst_time < end + _HOUR:
            return std_time.replace(fold=1)
        if std_time < start or dst_time >= end:
            return std_time
        if start <= std_time < end - _HOUR:
            return dst_time
        return std_time  # the missing hour: reached only by a gap time


def _new_york() -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:  # no tzdata on this machine (Windows without the tzdata package)
        return _UsEastern()


NEW_YORK = _new_york()


class EasternClock:
    """A real instant (UTC), handed to the scheduler as New York local time."""

    def __init__(self, utc: datetime) -> None:
        self.utc = utc
        self._origin = utc

    def __call__(self) -> datetime:
        return self.utc.astimezone(NEW_YORK)

    @property
    def now(self) -> datetime:
        return self()

    def monotonic(self) -> float:
        return 1000.0 + (self.utc - self._origin).total_seconds()

    def advance(self, seconds: float) -> None:
        self.utc += timedelta(seconds=seconds)


# --- R7: a corrupt state row must not stop the frame from booting ------------------------


def test_probe_r7_corrupt_json_in_state_row_does_not_crash_startup(make_rig) -> None:
    """R7/R6: ``db.get_state`` does ``json.loads`` on the stored text; a row that
    is not JSON at all (torn write, SD-card bit rot, a hand edit) makes
    ``Scheduler.__init__`` raise, so ``create_app``'s lifespan fails and the
    frame never boots: no rotation, no keep-alive, systemd restart loop.
    The documented promise is "invalid data gives defaults"."""
    rig = make_rig()
    rig.tick()
    with rig.db._lock:
        rig.db._conn.execute(
            "UPDATE state SET value = ? WHERE key = ?", ('{"slot_index": 1, "last_att', STATE_KEY)
        )
        rig.db._conn.execute(
            "UPDATE state SET value = ? WHERE key = ?", ("not json", "last_refresh_at")
        )
    sched = rig.new_scheduler()  # raised json.JSONDecodeError as reviewed
    assert sched._state.slot_index == 0
    rig.sched = sched
    rig.clock.advance(seconds=200)
    rig.tick()
    assert rig.shows == 2


# --- R1/R2: the floor and the show-now minimum are wall-clock only -----------------------


def test_probe_r1_last_attempt_prewrite_is_the_time_the_panel_is_touched(make_rig) -> None:
    """R1 (attack angle 1): ``_refresh`` persists ``last_attempt_at = now``
    before ``init()``, but ``now`` is the tick's start, taken BEFORE the render.
    A render that takes 100 s (network app, cold dither, up to the 180 s render
    timeout) followed by a crash inside ``show()`` (process killed, power cut)
    leaves a persisted attempt 100 s older than the real one, so after the
    restart the floor ends ~100 s early.  The pre-write must be the time the
    panel is actually touched."""
    rig = make_rig()
    real_render = rig.apps["alpha"].render

    def slow_render(ctx):
        rig.clock.advance(seconds=100)
        return real_render(ctx)

    rig.apps["alpha"].render = slow_render  # type: ignore[method-assign]
    seen: dict[str, object] = {}
    real_init = rig.display.init

    def init_and_record() -> None:
        # what a restart after a crash right here would find on disk
        seen["persisted"] = rig.db.get_state(STATE_KEY)["last_attempt_at"]
        seen["touched_at"] = rig.clock.now
        real_init()

    rig.display.init = init_and_record  # type: ignore[method-assign]
    rig.tick()
    assert seen["touched_at"] == START + timedelta(seconds=100)
    assert seen["persisted"] == iso(START + timedelta(seconds=100))  # was iso(START)


def _jump_rig(make_rig):
    """A default rig whose scheduler runs on a ``JumpClock`` starting at START."""
    rig = make_rig()
    clock = JumpClock(START)
    rig.clock = clock
    rig.sched = rig.new_scheduler(now_fn=clock, monotonic_fn=clock.monotonic)
    return rig


def test_probe_r1_small_forward_clock_step_shortens_the_floor(make_rig) -> None:
    """R1: wall-clock jumps up to ``CLOCK_JUMP_SECONDS`` (60 s) are not
    compensated, and the floor is measured on the wall clock only.  NTP
    stepping the clock +59 s (e.g. after a quick reboot, timesyncd's saved
    clock is ~1 min behind) plus any early wake (a settings PUT, show-now,
    Phase 3's ``wake()`` after an upload) lets a rotation refresh happen 125 s
    after the previous one with a 180 s floor.  A monotonic anchor for the
    last attempt would close this for jumps of any size."""
    rig = _jump_rig(make_rig)
    rig.tick()
    assert rig.shows == 1
    rig.clock.advance(125)  # real time: 125 s after the first refresh
    rig.clock.jump(59)  # NTP step: the wall clock now says +184 s
    rig.tick()
    assert rig.shows == 1, "refreshed 125 s after the previous refresh (floor 180 s)"


def test_probe_r2_small_forward_clock_step_lets_show_now_fire_within_30_s(make_rig) -> None:
    """R2: the show-now minimum (30 s, "to let the panel finish") is wall-clock
    only too: a +45 s NTP step right after a refresh lets a show-now fire 5 s
    after the previous refresh ended."""
    rig = _jump_rig(make_rig)
    rig.tick()
    rig.clock.advance(5)
    rig.clock.jump(45)
    rig.sched.show_now(app_id="beta")
    rig.tick()
    assert rig.shows == 1, "show-now fired 5 s after the previous refresh"


# --- R3: NTP's first step after a power cut must not postpone the keep-alive -------------


def test_probe_r3_ntp_step_after_restart_does_not_postpone_the_keepalive(make_rig) -> None:
    """R3 (attack angle 3): the Pi has no RTC, so after a power cut it boots
    with the clock timesyncd saved before the cut (hours behind).  The first
    NTP step after boot is seen as a clock jump and EVERY anchor is shifted by
    it, including ``last_refresh_at`` that was loaded from the database and
    was written in true time before the cut.  With the rotation paused the
    keep-alive is then postponed by the whole outage: below the panel was
    last refreshed at 12:00 (true), the keep-alive is due at 08:00 the next
    day (20 h), but the scheduler waits until 17:00 (29 h).

    Shifting forward is only right for anchors set during this run; anchors
    from disk (or the keep-alive anchor in general) must not move later."""
    rig = make_rig({"panel": {"keepalive_hours": 20}})
    rig.tick()  # 12:00 true time: last refresh
    rig.sched.pause()

    # power cut at 13:00 (timesyncd saved the clock then); power back at 22:00
    clock = JumpClock(START + timedelta(hours=1))  # what the boot clock says
    rig.sched = rig.new_scheduler(now_fn=clock, monotonic_fn=clock.monotonic)
    rig.clock = clock  # type: ignore[assignment]
    rig.tick()
    clock.advance(20)
    clock.jump(9 * 3600)  # NTP: the wall clock steps to the true time, 22:00:20
    rig.tick()
    deadline = START + timedelta(hours=20)  # 08:00 tomorrow, true time
    assert rig.snapshot()["rotation"]["keepalive_at"] <= iso(deadline)
    while clock.now < deadline + timedelta(minutes=5):
        delay = rig.tick()
        clock.advance(delay)
    assert rig.shows == 2, "no keep-alive within keepalive_hours of the last refresh"


# --- R3/R6: a database failure starves the keep-alive ------------------------------------


def test_probe_r3_photo_listing_failure_does_not_starve_the_keepalive(make_rig) -> None:
    """R3/R6: the scheduler lists photos (``db.list_photos``) outside any
    try-block while planning.  If that read fails persistently (SD-card
    corruption of the photos table, a photo row whose recipe JSON does not
    parse in ``Photo.from_row``, a Phase 3 migration bug) every ``tick`` raises
    from ``_plan``: ``_run`` logs it and retries every 30 s forever, and no
    refresh of any kind happens, not even the keep-alive re-show of the last
    frame (which needs no database).  ``snapshot()`` raises too, so
    ``/api/status`` is a 500."""
    rig = make_rig(
        {"panel": {"keepalive_hours": 1}, "rotation": [slot("alpha", 1), slot("photos", 1)]}
    )
    rig.tick()  # alpha at 12:00
    assert rig.shows == 1

    def broken(include_excluded: bool = True):
        raise sqlite3.DatabaseError("database disk image is malformed")

    rig.db.list_photos = broken  # type: ignore[method-assign]
    end = START + timedelta(hours=1, minutes=5)
    while rig.clock.now < end:
        try:
            delay = rig.tick()
        except Exception:  # what Scheduler._run does
            delay = CRASH_DELAY
        rig.clock.advance(seconds=delay)
    assert rig.shows >= 2, "the panel went past keepalive_hours without any refresh"


# --- DST with a DST-aware local zone (zoneinfo America/New_York) --------------------------


def test_probe_r3_spring_forward_with_a_dst_aware_zone_does_not_move_the_keepalive(
    make_rig,
) -> None:
    """R3 (attack angle 4, minor; only with a DST-aware ``now_fn`` such as
    ``zoneinfo``, which the ``now_fn`` contract "aware local time" allows;
    production's ``local_now()`` returns a fixed offset and is not affected).
    ``_observe_clock`` subtracts two datetimes that share one tzinfo, which
    Python does on the WALL clock, so 01:50 EST -> 03:10 EDT (20 real minutes)
    looks like a +1 h jump and every anchor is shifted an hour forward.
    Anchors loaded from disk are UTC, so the shift is real: the keep-alive of
    a paused frame moves from 20 h to 21 h after the last refresh (and the
    floor of a just-restarted scheduler grows by an hour).  Compare instants
    in UTC (``now.astimezone(timezone.utc)``) in ``_observe_clock``."""
    clock = EasternClock(datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc))  # 01:00 EST
    rig = make_rig({"panel": {"keepalive_hours": 20}}, clock=FakeClock(clock()))
    rig.clock = clock  # type: ignore[assignment]
    rig.sched = rig.new_scheduler(now_fn=clock, monotonic_fn=clock.monotonic)
    rig.sched.tick(clock())  # a refresh at 01:00 EST = 06:00 UTC
    rig.sched.pause()
    rig.sched = rig.new_scheduler(now_fn=clock, monotonic_fn=clock.monotonic)  # restart
    clock.advance(50 * 60)
    rig.sched.tick(clock())  # 01:50 EST
    before = rig.snapshot()["rotation"]["keepalive_at"]
    assert before == "2026-03-09T02:00:00.000+00:00"
    clock.advance(20 * 60)
    assert clock().hour == 3  # 03:10 EDT: only 20 minutes really passed
    rig.sched.tick(clock())
    assert rig.snapshot()["rotation"]["keepalive_at"] == before


def test_probe_quiet_until_is_never_before_now_in_the_repeated_hour() -> None:
    """``quiet_window_active`` with a DST-aware zone: at 01:15 EST (the second
    01:15, fold=1) a 22:00-01:30 window is still "active" on the wall clock,
    but ``until = now + (end - wall)`` drops the fold and returns 01:30 EDT,
    45 minutes BEFORE ``now``; the UI countdown (``rotation.quiet_until``)
    then points into the past."""
    qh = QuietHours(enabled=True, start="22:00", end="01:30", days=(0, 1, 2, 3, 4, 5, 6))
    now = datetime(2026, 11, 1, 6, 15, tzinfo=timezone.utc).astimezone(NEW_YORK)
    assert now.fold == 1 and now.hour == 1 and now.minute == 15
    active, until = quiet_window_active(qh, now)
    if active:
        assert until is not None
        # compare instants: ``>`` between two datetimes of one tzinfo is wall-clock only
        assert until.astimezone(timezone.utc) > now.astimezone(timezone.utc), (
            f"quiet_until {iso(until)} is before now {iso(now)}"
        )


# --- status reporting: next_at evaluates quiet hours on the UTC wall clock -----------------


def test_probe_next_at_uses_the_local_wall_clock_for_quiet_hours(make_rig) -> None:
    """``_next_rotation_at`` passes ``max(plan.due_at, now)`` to
    ``quiet_window_active``; ``due_at`` is built from the UTC anchors, so the
    window is evaluated on the UTC wall clock.  Local zone UTC-4, quiet
    23:00-07:00 local, a slot ending at 23:05 local (03:05 UTC): ``next_at``
    should be 07:00 local, but 03:05 UTC is inside a 23:00-07:00 *UTC* window
    that ends at 07:00 UTC, so 03:00 local is reported (UI only; the decision
    core uses the local ``now``).  Before a restart the in-memory anchors carry
    the local zone of ``now``, which hides the bug."""
    rig = make_rig(
        {
            "quiet_hours": {"enabled": True, "start": "23:00", "end": "07:00", "days": [0, 1]},
            "rotation": [slot("alpha", 10), slot("beta", 10)],
        },
        clock=FakeClock(at(19, 22, 55)),
    )
    rig.tick()  # alpha at 22:55 local; its slot ends at 23:05 local, inside quiet hours
    assert rig.snapshot()["rotation"]["next_at"] == iso(at(20, 7))
    rig.sched = rig.new_scheduler()  # after a restart the anchors are UTC datetimes
    assert rig.snapshot()["rotation"]["next_at"] == iso(at(20, 7)), "reported 03:00 local"


# --- R7/R3 (mock display): the frame file a keep-alive reads after a restart -------------


def test_probe_keepalive_frame_on_disk_is_white_after_clear_succeeded_but_show_failed(
    make_rig,
) -> None:
    """R7/R3 (mock display only, minor): with ``clear_before_refresh`` the
    MockDisplay writes an all-white ``last_frame.bin`` on ``clear()``; if
    ``show()`` then fails, the scheduler keeps its in-memory frame but the
    file on disk is now white, so after a restart the keep-alive re-shows a
    white panel instead of the last image.  The scheduler should own
    ``last_frame.bin`` (or restore it after a failed refresh)."""
    rig = make_rig({"display": {"clear_before_refresh": True}, "paused": False})
    rig.tick()
    good = rig.display.last_frame.copy()
    assert not (good == 1).all()
    rig.display.fail_show = True
    rig.clock.advance(seconds=180)
    rig.tick()  # clear() wrote white to last_frame.bin, then show() failed
    rig.display.fail_show = False
    rig.store.patch({"paused": True, "panel": {"keepalive_hours": 1}})
    rig.sched = rig.new_scheduler()  # restart
    rig.clock.set(START + timedelta(hours=1, minutes=5))
    rig.tick()
    assert rig.source()["type"] == "keepalive"
    assert (rig.display.last_frame == good).all(), "keep-alive re-showed a white frame"


# --- R6: a hung render must not leave a backlog on the one render thread -------------------


def test_probe_r6_timed_out_renders_are_not_left_queued(make_rig) -> None:
    """R6 (minor): ``_render`` does ``executor.submit(job).result(timeout)`` and
    never cancels the future.  While one app hangs (a network call that ignores
    its timeout), every later render, of every app and every preview, queues
    behind it on the single render thread and times out too, and each pass
    leaves its jobs queued: after 40 ticks 39 abandoned renders are waiting,
    and when the hang clears they all run back to back (minutes of CPU on a
    Pi Zero, previews starved behind them).  Cancelling the future on timeout
    (or not submitting while a timed-out render still runs) avoids that."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    apps = fake_apps()
    gate = threading.Event()
    real = apps["alpha"].render

    def hung(ctx):
        gate.wait(30)
        return real(ctx)

    apps["alpha"].render = hung  # type: ignore[method-assign]
    rig = make_rig(apps=apps)
    executor = ThreadPoolExecutor(max_workers=1)
    rig.sched = rig.new_scheduler(executor=executor, render_timeout=0.05)
    try:
        for _ in range(40):
            rig.clock.advance(seconds=rig.tick())
    finally:
        gate.set()  # the hang clears ...
        executor.shutdown(wait=True)  # ... and whatever is still queued runs now
    ran = sum(len(a.renders) for a in apps.values())
    # only the hung render itself should run; as reviewed, 40 stale renders do
    assert ran <= 2, f"{ran} abandoned renders ran back to back after the hang cleared"


# === review round 2 ======================================================================


def _fake_dither_engine(monkeypatch: pytest.MonkeyPatch, result: np.ndarray) -> None:
    """Install a stand-in for the Phase 2 engine (``inkframe.render.dither``)
    whose ``dither()`` returns ``result``."""
    fake = types.ModuleType("inkframe.render.dither")
    fake.dither = lambda rgb, palette, method, **kw: result  # type: ignore[attr-defined]
    try:
        importlib.import_module("inkframe.render")
    except ImportError:  # Phase 2 not merged yet
        monkeypatch.setitem(sys.modules, "inkframe.render", types.ModuleType("inkframe.render"))
    monkeypatch.setitem(sys.modules, "inkframe.render.dither", fake)


class _DitheringApp(FakeApp):
    """A ``needs_dither = True`` app (like a photo pipeline returning RGB)."""

    needs_dither = True

    def render(self, ctx):  # type: ignore[override]
        self.renders.append(ctx.item_id)
        return Image.new("RGB", (600, 400), (120, 30, 200))


def test_probe_r6_an_invalid_dithered_frame_is_skipped_like_a_render_failure(
    make_rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6 (attack angle 9): ``render_to_frame`` validates an index array and
    an undithered RGB image, but returns the dither engine's output
    unchecked, and ``Scheduler._render`` does not validate the frame either.
    A bad frame from that path (here the engine returns (600, 400), i.e.
    rows and columns swapped) first fails inside ``display.show()``
    (``validate_frame`` raises ValueError), so it is recorded as a *display*
    failure: the rotation never moves past the slot, it is re-rendered and
    re-attempted every floor forever, and with ``clear_before_refresh`` every
    attempt runs ``clear()`` first, so the panel is flooded white every 3 min
    and stays white.  As reviewed: 30 min -> 10 clears, 1 show (alpha at
    12:00), beta rendered 9 times, gamma never shown.  The frame must be
    validated before the panel is touched, so it is skipped like any app
    failure (the next slot is tried, the panel keeps its image)."""
    _fake_dither_engine(monkeypatch, np.zeros((600, 400), dtype=np.uint8))
    apps = fake_apps()
    apps["beta"] = _DitheringApp("beta", 3)
    rig = make_rig({"display": {"clear_before_refresh": True}}, apps=apps)
    rig.run_until(START + timedelta(minutes=15))
    err = rig.snapshot()["panel"]["last_error"]
    assert err is not None and err["app_id"] == "beta"
    assert err["kind"] == "render", "a bad frame was treated as a panel failure"
    assert apps["gamma"].renders, "the rotation got stuck on beta"
    assert rig.display.clear_count == rig.display.show_count, "clear() without a show()"
    assert not (rig.display.last_frame == WHITE).all(), "the panel was left white"


def test_probe_r1_a_corrupt_state_row_does_not_drop_the_floor(make_rig) -> None:
    """R1/R2 + R7 (attack angle 7): when the ``scheduler`` row is unreadable
    (here: not JSON; also a valid blob without ``last_attempt_at``) the state
    falls back to defaults, and ``last_refresh_at`` is recovered from its
    mirror key, but ``last_attempt_at`` stays None, so after the restart
    neither the floor nor the show-now minimum applies: below the panel was
    refreshed at 12:00 (the mirror says so) and the restarted scheduler
    refreshes it again at 12:00:10.  A missing attempt anchor must fall back
    to the last refresh (an attempt happened at least then)."""
    rig = make_rig()
    rig.tick()  # alpha at 12:00
    with rig.db._lock:
        rig.db._conn.execute(
            "UPDATE state SET value = ? WHERE key = ?", ('{"slot_index": 1, "last_att', STATE_KEY)
        )
    rig.clock.advance(seconds=10)
    rig.sched = rig.new_scheduler()  # restart at 12:00:10
    assert rig.snapshot()["panel"]["last_refresh_at"] == iso(START)
    rig.tick()
    assert rig.shows == 1, "refreshed 10 s after the previous refresh (floor 180 s)"
    info = rig.sched.show_now(app_id="beta")
    assert info["fires_at"] == iso(START + timedelta(seconds=30)), "show-now minimum ignored"
    rig.clock.set(START + timedelta(seconds=180))
    rig.tick()
    assert rig.shows == 2


def test_probe_r6_an_app_raising_systemexit_does_not_kill_the_scheduler_thread(
    tmp_path: Path,
) -> None:
    """R6 "never crash the thread": ``_show_item`` catches ``Exception`` and
    ``_run`` catches ``Exception``, but a render that raises ``SystemExit``
    (a library calling ``sys.exit()`` on a fatal error; the render executor
    hands any ``BaseException`` back through ``future.result()``) escapes
    both: the scheduler thread ends silently (``threading`` ignores
    SystemExit), the web UI keeps running, and the panel is never refreshed
    again, keep-alive included, until the service restarts.  As reviewed the
    thread is dead after the first tick and beta is never shown.  It must be
    skipped like any render failure (the next slot a second later); merely
    surviving in ``_run`` would retry the same slot every 30 s forever."""
    paths = DataPaths(tmp_path / "data").ensure_dirs()
    store = SettingsStore(paths.settings_file)
    store.patch(
        {"quiet_hours": {"enabled": False}, "rotation": [slot("alpha", 1), slot("beta", 1)]}
    )
    db = Database(paths.db_file)

    class Exiting(FakeApp):
        def render(self, ctx):  # type: ignore[override]
            self.renders.append(ctx.item_id)
            raise SystemExit(1)

    apps = {"alpha": Exiting("alpha", 2), "beta": FakeApp("beta", 3)}
    display = RecordingDisplay(paths.root)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render")
    sched = Scheduler(display, store, db, paths, apps, executor=executor)
    sched.start()
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and display.show_count == 0:
            time.sleep(0.02)
        assert apps["alpha"].renders, "alpha was never rendered"
        assert sched.running, "the scheduler thread died (SystemExit from an app render)"
        assert display.show_count == 1, "beta was never shown after alpha failed"
    finally:
        sched.stop(timeout=10)
        executor.shutdown(wait=True)
        db.close()


def test_probe_r6_a_failed_refresh_is_repaired_when_the_content_is_static(make_rig) -> None:
    """R6/R3 (minor): after a failed attempt the panel's content is unknown
    (with ``clear_before_refresh`` it is white once ``clear()`` succeeded and
    ``show()`` failed), but ``state.showing`` still names the old content.  A
    failed show-now is not retried, and the rotation's "already showing"
    shortcut then skips every refresh of that same static content, so a
    one-photo frame (or the "no photos" card) stays white until the
    keep-alive, 20 h after the last success.  As reviewed: no refresh at all
    in the 5 h after the failure.  A failed attempt should forget what the
    panel shows (or retry), so the next rotation change repairs it."""
    rig = make_rig(
        {
            "display": {"clear_before_refresh": True},
            "rotation": [slot("photos", 10, dwell_minutes=3)],
        }
    )
    photo = rig.add_photo("only.jpg", "2026-10-01T10:00:00.000+00:00")
    rig.tick()  # the photo at 12:00
    rig.clock.advance(minutes=5)
    rig.display.fail_show = True
    rig.sched.show_now(photo_id=photo.id)
    rig.tick()  # 12:05: clear() whitened the panel, then show() timed out
    rig.display.fail_show = False
    assert (rig.display.last_frame == WHITE).all()
    rig.run_until(rig.clock.now + timedelta(minutes=30))
    assert not (rig.display.last_frame == WHITE).all(), "panel left white until the keep-alive"


def test_probe_r3_a_floor_longer_than_the_keepalive_cannot_postpone_it(make_rig) -> None:
    """R3 vs R1 (minor, settings): ``panel.refresh_floor_seconds`` may be up
    to 86400 s and ``keepalive_hours`` as low as 1, and nothing relates the
    two, so a floor longer than the keep-alive is accepted; the scheduler then
    holds the keep-alive back to the floor (``_plan`` step (b)), i.e. R3 is
    silently not enforced: below, paused with a 1 h keep-alive, the panel is
    next refreshed after 2 h.  Either the settings reject such a floor (the
    test accepts a ValueError) or the keep-alive comes within
    ``keepalive_hours``."""
    panel = {"refresh_floor_seconds": 7200, "show_now_min_seconds": 30, "keepalive_hours": 1}
    try:
        rig = make_rig({"panel": panel})
    except ValueError:
        return  # rejected by the settings: R1 and R3 can both hold
    rig.tick()  # 12:00
    rig.sched.pause()
    refreshes = rig.run_until(START + timedelta(hours=1, minutes=5))
    assert refreshes, "no keep-alive within keepalive_hours (it waited for the 2 h floor)"


# === review round 3 ======================================================================


def test_probe_r3_r6_a_photo_whose_recipe_is_not_an_object_does_not_stall_the_frame(
    make_rig,
) -> None:
    """R3/R6 (critical; needs a photo row whose ``recipe`` column holds valid
    JSON that is not an object, e.g. ``"atkinson"`` or ``[1]``: a Phase 3 bug
    or a direct SQL write, since ``Database.set_photo_recipe`` only writes
    dicts).  ``Photo.from_row`` accepts it, the photo renders fine, and then
    ``Scheduler._show_item`` computes the log ``detail`` OUTSIDE its try-block:
    ``(item.photo.recipe or settings.default_recipe).get("method")`` raises
    AttributeError.  That escapes ``tick``; ``_run`` retries in 30 s and the
    plan is the same photo again (it was never recorded as failed, and the
    shuffle pick stays reserved), so the rotation is stuck on it for good.
    Because a due rotation refresh is preferred over the keep-alive re-show
    (``_plan`` step (b)), the keep-alive never fires either.  As reviewed:
    from 12:00 to 13:10 with a 1 h keep-alive, ONE refresh (alpha at 12:00),
    134 renders of the same photo, a traceback every 30 s.  Anything that can
    raise after the render must be handled like a render failure (the slot
    skipped), and a non-mapping recipe should fall back to the default."""
    rig = make_rig(
        {"panel": {"keepalive_hours": 1}, "rotation": [slot("alpha", 1), slot("photos", 10)]}
    )
    photo = rig.add_photo("odd.jpg", "2026-10-01T10:00:00.000+00:00")
    with rig.db._lock:
        rig.db._conn.execute("UPDATE photos SET recipe = ? WHERE id = ?", ('"atkinson"', photo.id))
    rig.tick()  # alpha at 12:00
    end = START + timedelta(hours=1, minutes=10)
    crashes = 0
    while rig.clock.now < end:
        try:
            delay = rig.tick()
        except Exception:  # what Scheduler._run does: log, retry in CRASH_DELAY
            crashes += 1
            delay = CRASH_DELAY
        rig.clock.advance(seconds=delay)
    assert rig.shows >= 2, "no refresh for 70 min (keep-alive 1 h): stuck on one photo"
    assert crashes == 0, f"tick raised {crashes} times"
    assert len(rig.apps["photos"].renders) < 30, "the same photo was re-rendered every 30 s"


def test_probe_r6_a_failed_refresh_while_paused_is_repaired_before_the_keepalive(
    make_rig,
) -> None:
    """R6/R3 (minor, ``clear_before_refresh`` on): round 2's ``panel_stale``
    repair happens only at the next *rotation change*.  While the rotation is
    paused (or in quiet hours) there is none, so after a refresh whose
    ``clear()`` worked and whose ``show()`` failed the panel stays white until
    the keep-alive, ``keepalive_hours`` (20 h) after the last success.  Same
    with quiet hours: a rotation refresh that fails at 22:58 leaves the frame
    white all night.  As reviewed: no refresh at all in the 2 h after the
    failure.  A failed last attempt (``last_attempt_at`` after
    ``last_refresh_at``) should make the keep-alive re-show of the last good
    frame due once the floor has passed, paused or quiet or not (R1 still
    holds: one attempt per floor at most)."""
    rig = make_rig({"display": {"clear_before_refresh": True}})
    rig.tick()  # alpha at 12:00
    rig.sched.pause()
    rig.clock.advance(minutes=5)
    rig.display.fail_show = True
    rig.sched.show_now(app_id="beta")
    rig.tick()  # 12:05: clear() whitened the panel, then show() timed out
    failed_at = rig.clock.now
    rig.display.fail_show = False
    assert (rig.display.last_frame == WHITE).all()
    refreshes = rig.run_until(rig.clock.now + timedelta(hours=2))
    times = [failed_at, *refreshes]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)]
    assert all(g >= 180 for g in gaps), "the repair must respect the floor (R1)"
    assert not (rig.display.last_frame == WHITE).all(), "white until the 20 h keep-alive"


def test_probe_r7_a_far_future_timestamp_in_the_state_row_does_not_stop_the_boot(
    make_rig,
) -> None:
    """R7 (minor; needs a hand-edited or otherwise mangled state row, since
    ``iso()`` only writes milliseconds and ``+00:00``): a timing anchor such
    as ``9999-12-31T23:59:59.999999+00:00`` (or ``9999-12-31T20:00:00-05:00``)
    parses, ``_clamp_future`` sees it is in the future and logs it with
    ``iso(t)``, which overflows (rounding up to the next millisecond, or the
    conversion to UTC, leaves year 9999): ``Scheduler.__init__`` raises
    OverflowError, so ``create_app``'s lifespan fails and the frame never
    boots (systemd restart loop, no keep-alive).  "Loading is tolerant:
    invalid data gives defaults" should hold for any parsable value too."""
    rig = make_rig()
    rig.tick()  # alpha at 12:00
    state = rig.db.get_state(STATE_KEY)
    state["slot_started_at"] = "9999-12-31T23:59:59.999999+00:00"
    rig.db.set_state(STATE_KEY, state)
    rig.sched = rig.new_scheduler()  # raised OverflowError as reviewed
    rig.clock.advance(seconds=200)
    rig.tick()
    assert rig.shows == 2
