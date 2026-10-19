"""The scheduler's decision core, driven by a fake clock (no thread, no sleeping).

Every test builds a ``Rig`` (see conftest): a real SettingsStore, Database and
MockDisplay on a temp dir, fake apps, and ``Scheduler.tick(now)`` called by
hand.  The panel-care rules under test are listed in ``inkframe.scheduler``.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from inkframe.config import SettingsStore
from inkframe.db import Database, Photo
from inkframe.display.mock import read_last_frame
from inkframe.display.palette import pack_frame
from inkframe.paths import DataPaths
from inkframe.scheduler import (
    BACKOFF_SECONDS,
    MAX_DELAY,
    RETRY_DELAY,
    STATE_KEY,
    Scheduler,
    SchedulerState,
    iso,
    pick_next_photo,
    validate_photo_options,
)
from tests.conftest import START, FakeApp, FakeClock, RecordingDisplay, fake_apps, slot

# --- R1: the refresh floor ----------------------------------------------------------------


def test_floor_enforced_between_rotation_refreshes(make_rig) -> None:
    rig = make_rig()  # three 1-minute slots, floor 180 s
    assert rig.tick() == 180  # alpha shown at once; next change waits for the floor
    assert rig.shows == 1 and rig.shown_id() == "alpha"

    rig.clock.advance(seconds=30)
    assert rig.tick() == 150  # the slot would end at +60 s; the floor says +180 s
    rig.clock.advance(seconds=120)
    assert rig.tick() == 30
    assert rig.shows == 1
    rig.clock.advance(seconds=30)
    rig.tick()
    assert rig.shows == 2 and rig.shown_id() == "beta"


def test_rotation_refreshes_never_closer_than_floor(make_rig) -> None:
    rig = make_rig({"panel": {"refresh_floor_seconds": 200, "show_now_min_seconds": 30}})
    refreshes = rig.run_until(START + timedelta(hours=3))
    assert len(refreshes) >= 50
    gaps = [(b - a).total_seconds() for a, b in zip(refreshes, refreshes[1:], strict=False)]
    assert min(gaps) >= 200
    # extra wake-ups (settings edits, API calls) at odd moments never sneak one in
    rng = random.Random(5)
    last = refreshes[-1]
    for _ in range(300):
        rig.clock.advance(seconds=rng.uniform(0.5, 40))
        before = rig.shows
        rig.tick()
        if rig.shows != before:
            assert (rig.clock.now - last).total_seconds() >= 200
            last = rig.clock.now


def test_settings_change_is_respected_on_next_tick(make_rig) -> None:
    rig = make_rig()
    rig.tick()
    rig.clock.advance(seconds=100)
    assert rig.tick() == 80
    rig.store.patch({"panel": {"refresh_floor_seconds": 600}})
    rig.clock.advance(seconds=90)  # +190 s: the old floor would allow a refresh
    assert rig.tick() == MAX_DELAY  # 410 s to go, capped: the scheduler looks again
    assert rig.shows == 1
    rig.clock.advance(seconds=MAX_DELAY)
    assert rig.tick() == 110
    assert rig.shows == 1
    rig.clock.advance(seconds=110)
    rig.tick()
    assert rig.shows == 2


# --- R4 + R6: display failures ----------------------------------------------------------------


def test_failed_display_refresh_counts_as_attempt(make_rig) -> None:
    rig = make_rig()
    rig.display.fail_show = True
    assert rig.tick() == 180  # the next attempt waits the whole floor
    assert rig.display.calls == ["init", "show", "sleep"]
    assert rig.display.awake is False and rig.display.state == "asleep"
    snap = rig.snapshot()
    assert snap["panel"]["last_attempt_at"] == iso(START)
    assert snap["panel"]["last_refresh_at"] is None
    assert snap["panel"]["last_source"] is None
    err = snap["panel"]["last_error"]
    assert err["kind"] == "display" and "DisplayTimeout" in err["message"]
    assert rig.db.get_state("last_refresh_at") is None

    rig.clock.advance(seconds=100)
    assert rig.tick() == 80
    assert rig.display.calls.count("init") == 1  # not hammered
    rig.clock.advance(seconds=80)
    rig.tick()
    assert rig.display.calls.count("init") == 2
    assert rig.display.calls[-1] == "sleep"

    rig.display.fail_show = False
    rig.clock.advance(seconds=180)
    rig.tick()
    snap = rig.snapshot()
    assert snap["panel"]["last_refresh_at"] == iso(rig.clock.now)
    assert rig.shows == 1 and rig.shown_id() == "alpha"  # the failed item was retried


def test_init_failure_still_sleeps_the_panel(make_rig) -> None:
    rig = make_rig()
    rig.display.fail_init = True
    assert rig.tick() == 180
    assert rig.display.calls == ["init", "sleep"]
    assert rig.display.awake is False
    assert "OSError" in rig.snapshot()["panel"]["last_error"]["message"]


def test_clear_before_refresh_calls_clear_before_show(make_rig) -> None:
    rig = make_rig({"display": {"clear_before_refresh": True}})
    rig.tick()
    assert rig.display.calls == ["init", "clear", "show", "sleep"]
    assert rig.display.clear_count == 1 and rig.shows == 1
    rig.store.patch({"display": {"clear_before_refresh": False}})
    rig.clock.advance(seconds=180)
    rig.tick()
    assert rig.display.calls[4:] == ["init", "show", "sleep"]


def test_every_refresh_is_init_show_sleep(make_rig) -> None:
    rig = make_rig({"panel": {"refresh_floor_seconds": 30}})
    rig.run_until(START + timedelta(minutes=30))
    calls = rig.display.calls
    assert len(calls) % 3 == 0 and len(calls) >= 30
    assert calls == ["init", "show", "sleep"] * (len(calls) // 3)
    assert rig.display.awake is False


def test_successful_refresh_persists_frame_and_mirrors(make_rig) -> None:
    rig = make_rig({"display": {"rotation": 270}})
    rig.tick()
    frame = rig.display.last_frame
    assert frame is not None
    assert (rig.data / "last_frame.bin").read_bytes() == pack_frame(frame, 270)
    assert (read_last_frame(rig.data, 270) == frame).all()
    assert (rig.data / "last_frame.png").is_file()
    assert rig.db.get_state("last_refresh_at") == iso(START)
    assert rig.db.get_state("last_source") == {"type": "app", "id": "alpha", "label": "Alpha"}
    assert rig.db.get_state(STATE_KEY)["frame_rotation"] == 270


def test_failed_first_refresh_leaves_no_white_frame_behind(make_rig) -> None:
    """With nothing to restore, what the driver wrote during a failed attempt
    (MockDisplay's white ``clear()``) is removed, so a keep-alive renders the
    current slot instead of re-showing a white "last frame"."""
    rig = make_rig({"display": {"clear_before_refresh": True}})
    rig.display.fail_show = True
    rig.tick()  # clear() wrote a white last_frame.bin, then show() failed
    assert rig.display.clear_count == 1 and rig.shows == 0
    assert not (rig.data / "last_frame.bin").exists()
    assert not (rig.data / "last_frame.png").exists()
    rig.display.fail_show = False
    rig.store.patch({"paused": True, "panel": {"keepalive_hours": 1}})
    rig.sched = rig.new_scheduler()  # restart
    rig.clock.set(START + timedelta(hours=1, minutes=5))
    rig.tick()
    assert rig.shows == 1 and rig.shown_id() == "alpha"
    assert (read_last_frame(rig.data) == rig.display.last_frame).all()
    assert not (rig.display.last_frame == 1).all()  # not white


# --- R2: show now --------------------------------------------------------------------


def test_show_now_bypasses_floor_but_not_minimum(make_rig) -> None:
    rig = make_rig({"rotation": [slot("alpha", 60), slot("beta", 60), slot("gamma", 60)]})
    rig.tick()
    assert rig.shown_id() == "alpha"

    rig.clock.advance(seconds=10)
    info = rig.sched.show_now(app_id="beta")
    assert info["kind"] == "app" and info["id"] == "beta"
    assert info["fires_at"] == iso(START + timedelta(seconds=30))
    assert rig.tick() == 20
    assert rig.shows == 1

    rig.clock.advance(seconds=5)
    rig.sched.show_now(app_id="gamma")  # replaces the pending request
    pending = rig.snapshot()["rotation"]["pending"]
    assert pending == {
        "kind": "app",
        "id": "gamma",
        "label": "Gamma",
        "fires_at": iso(START + timedelta(seconds=30)),
    }
    assert rig.tick() == 15

    rig.clock.advance(seconds=15)  # fires_at: well inside the 180 s floor
    rig.tick()
    assert rig.shows == 2 and rig.shown_id() == "gamma"
    assert rig.apps["beta"].renders == []  # the replaced request never rendered
    assert rig.snapshot()["rotation"]["pending"] is None

    # a second request right away waits show_now_min_seconds after this attempt
    info = rig.sched.show_now(app_id="alpha")
    assert info["fires_at"] == iso(rig.clock.now + timedelta(seconds=30))
    assert rig.tick() == 30


def test_show_now_jumps_the_rotation_to_the_apps_slot(make_rig) -> None:
    rig = make_rig({"rotation": [slot("alpha", 10), slot("beta", 10), slot("gamma", 10)]})
    rig.tick()
    rig.clock.advance(seconds=60)
    rig.sched.show_now(app_id="beta")
    rig.tick()
    current = rig.snapshot()["rotation"]["current"]
    assert current["app_id"] == "beta" and current["started_at"] == iso(rig.clock.now)
    assert rig.snapshot()["rotation"]["next"]["app_id"] == "gamma"


def test_show_now_of_an_app_outside_the_rotation_keeps_timing(make_rig) -> None:
    rig = make_rig({"rotation": [slot("alpha", 10), slot("beta", 10, enabled=False)]})
    rig.tick()
    started = rig.snapshot()["rotation"]["current"]["started_at"]
    rig.clock.advance(seconds=60)
    rig.sched.show_now(app_id="beta")
    rig.tick()
    assert rig.shown_id() == "beta"
    current = rig.snapshot()["rotation"]["current"]
    assert current["app_id"] == "alpha" and current["started_at"] == started


def test_show_now_validates_ids(make_rig) -> None:
    rig = make_rig()
    excluded = rig.add_photo("hidden.jpg", "2026-10-01T00:00:00.000+00:00", excluded=True)
    with pytest.raises(KeyError, match="unknown app"):
        rig.sched.show_now(app_id="nope")
    with pytest.raises(KeyError, match="unknown photo"):
        rig.sched.show_now(photo_id="0" * 32)
    with pytest.raises(ValueError):
        rig.sched.show_now()
    with pytest.raises(ValueError):
        rig.sched.show_now(app_id="alpha", photo_id=excluded.id)
    # excluded photos can still be shown explicitly
    info = rig.sched.show_now(photo_id=excluded.id)
    assert info["kind"] == "photo" and info["label"] == "hidden.jpg"
    rig.tick()
    assert rig.source() == {"type": "photo", "id": excluded.id, "label": "hidden.jpg"}
    assert rig.apps["photos"].renders == [excluded.id]


def test_show_next_shows_what_snapshot_announced(make_rig) -> None:
    rig = make_rig({"rotation": [slot("alpha", 30), slot("beta", 30), slot("gamma", 30)]})
    rig.tick()
    rig.clock.advance(seconds=40)
    announced = rig.snapshot()["rotation"]["next"]
    assert announced["app_id"] == "beta"
    info = rig.sched.show_next()
    assert info["kind"] == "next" and info["label"] == "Beta"
    rig.tick()
    assert rig.shown_id() == "beta"
    assert rig.snapshot()["rotation"]["current"]["app_id"] == "beta"


def test_show_now_render_failure_is_recorded_not_retried(make_rig) -> None:
    rig = make_rig(apps=fake_apps("beta"))
    rig.tick()
    rig.clock.advance(seconds=40)
    rig.sched.show_now(app_id="beta")
    assert rig.tick() == RETRY_DELAY
    assert rig.shows == 1
    assert rig.snapshot()["panel"]["last_error"]["app_id"] == "beta"
    assert rig.snapshot()["rotation"]["pending"] is None


def test_show_now_during_a_render_counts_down_from_the_refresh(make_rig) -> None:
    """While a render or refresh is in flight, ``fires_at`` is measured from
    the attempt that is about to end, not from the previous one."""
    rig = make_rig({"rotation": [slot("alpha", 60), slot("beta", 60)]})
    real = rig.apps["alpha"].render
    seen: dict[str, object] = {}

    def slow(ctx):
        rig.clock.advance(seconds=100)  # a slow network app
        seen["info"] = rig.sched.show_now(app_id="beta")
        seen["snapshot"] = rig.snapshot()["rotation"]["pending"]["fires_at"]
        return real(ctx)

    rig.apps["alpha"].render = slow  # type: ignore[method-assign]
    assert rig.tick() == 30  # alpha refreshed at 12:01:40; beta 30 s after that
    fires_at = iso(START + timedelta(seconds=130))
    assert seen["info"]["fires_at"] == seen["snapshot"] == fires_at  # was 12:01:40
    assert rig.snapshot()["rotation"]["pending"]["fires_at"] == fires_at


# --- R6: app render failures --------------------------------------------------------------


def test_render_failure_skips_to_next_slot(make_rig, caplog: pytest.LogCaptureFixture) -> None:
    rig = make_rig({"panel": {"refresh_floor_seconds": 30}}, apps=fake_apps("beta"))
    rig.tick()
    alpha_frame = rig.display.last_frame.copy()
    alpha_bin = (rig.data / "last_frame.bin").read_bytes()

    rig.clock.advance(seconds=60)
    with caplog.at_level(logging.ERROR, logger="inkframe.scheduler"):
        assert rig.tick() == RETRY_DELAY
    assert any(r.exc_info for r in caplog.records if "beta" in r.getMessage())
    assert rig.shows == 1  # the panel kept alpha
    assert (rig.display.last_frame == alpha_frame).all()
    assert (rig.data / "last_frame.bin").read_bytes() == alpha_bin
    err = rig.snapshot()["panel"]["last_error"]
    assert err["kind"] == "render" and err["app_id"] == "beta"
    assert "beta is broken" in err["message"]

    rig.clock.advance(seconds=RETRY_DELAY)
    rig.tick()
    assert rig.shows == 2 and rig.shown_id() == "gamma"
    assert rig.snapshot()["panel"]["last_error"]["app_id"] == "beta"  # still reported
    assert rig.apps["beta"].renders == [None]

    # next pass: beta is tried again (and skipped again)
    rig.run_until(rig.clock.now + timedelta(minutes=3))
    assert len(rig.apps["beta"].renders) == 2


def test_all_apps_failing_backs_off(make_rig) -> None:
    rig = make_rig(apps=fake_apps("alpha", "beta", "gamma"))
    assert rig.tick() == RETRY_DELAY
    rig.clock.advance(seconds=1)
    assert rig.tick() == RETRY_DELAY
    rig.clock.advance(seconds=1)
    assert rig.tick() == BACKOFF_SECONDS  # all three failed in one pass
    renders = sum(len(a.renders) for a in rig.apps.values())
    assert renders == 3

    rig.clock.advance(seconds=1)
    assert rig.tick() == BACKOFF_SECONDS - 1  # waiting, not rendering
    assert sum(len(a.renders) for a in rig.apps.values()) == 3

    rig.run_until(rig.clock.now + timedelta(minutes=10))
    renders = sum(len(a.renders) for a in rig.apps.values())
    assert renders <= 3 * (10 + 2)  # about one pass a minute, never a spin
    assert rig.shows == 0 and rig.display.calls == []
    assert rig.snapshot()["panel"]["last_error"]["kind"] == "render"


def test_all_apps_failing_keeps_the_previous_frame(make_rig) -> None:
    rig = make_rig()
    rig.tick()
    for app in rig.apps.values():
        app.fail = True
    rig.run_until(rig.clock.now + timedelta(minutes=15))
    assert rig.shows == 1 and rig.shown_id() == "alpha"


def test_render_raising_systemexit_is_a_render_failure(make_rig) -> None:
    """R6: a ``BaseException`` that is not an ``Exception`` (here a library
    calling ``sys.exit()``) is skipped like any render failure, also when the
    render runs inline (the thread case is in test_scheduler_probe.py)."""

    class Exiting(FakeApp):
        def render(self, ctx):  # type: ignore[override]
            self.renders.append(ctx.item_id)
            raise SystemExit(3)

    apps = fake_apps()
    apps["alpha"] = Exiting("alpha", 2)
    rig = make_rig(apps=apps)
    assert rig.tick() == RETRY_DELAY
    err = rig.snapshot()["panel"]["last_error"]
    assert err["kind"] == "render" and err["app_id"] == "alpha"
    assert err["message"] == "SystemExit: 3"
    rig.clock.advance(seconds=RETRY_DELAY)
    rig.tick()
    assert rig.shows == 1 and rig.shown_id() == "beta"


def test_keepalive_due_inside_a_backoff_is_not_delayed(make_rig) -> None:
    """After a failed render ``tick`` re-plans instead of returning the retry
    or backoff delay as is: a keep-alive that comes due inside a 60 s backoff
    fires on time, not up to a minute late."""
    rig = make_rig({"panel": {"keepalive_hours": 1}})
    rig.tick()  # alpha at 12:00; the keep-alive is due at 13:00
    for app in rig.apps.values():
        app.fail = True
    rig.clock.set(START + timedelta(minutes=59, seconds=30))
    assert rig.tick() == RETRY_DELAY  # beta fails ...
    rig.clock.advance(seconds=RETRY_DELAY)
    assert rig.tick() == RETRY_DELAY  # ... gamma fails ...
    rig.clock.advance(seconds=RETRY_DELAY)
    # ... alpha fails: the 60 s backoff would end at 13:00:32
    assert rig.tick() == pytest.approx(28)
    rig.clock.advance(seconds=28)
    rig.tick()
    assert rig.shows == 2 and rig.source()["type"] == "keepalive"
    assert rig.snapshot()["panel"]["last_refresh_at"] == iso(START + timedelta(hours=1))


def test_unreadable_photo_list_skips_the_photos_slot(
    make_rig, caplog: pytest.LogCaptureFixture
) -> None:
    """R6: a photo list that cannot be read (SD-card corruption, a row that
    does not parse) fails the photos slot like a render that raised; planning
    and ``snapshot()`` never raise, and the error is logged once."""
    rig = make_rig({"rotation": [slot("photos", 5), slot("alpha", 1)]})
    rig.add_photo("one.jpg", "2026-10-01T10:00:00.000+00:00")

    def broken(include_excluded: bool = True):
        raise sqlite3.DatabaseError("database disk image is malformed")

    real = rig.db.list_photos
    rig.db.list_photos = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="inkframe.scheduler"):
        assert rig.tick() == RETRY_DELAY
        for _ in range(3):
            rig.snapshot()
    assert rig.shows == 0 and rig.apps["photos"].renders == []
    err = rig.snapshot()["panel"]["last_error"]
    assert err["kind"] == "render" and err["app_id"] == "photos"
    assert "photo list" in err["message"] and "malformed" in err["message"]
    logged = [r for r in caplog.records if "could not read the photo list" in r.getMessage()]
    assert len(logged) == 1 and logged[0].exc_info
    rig.clock.advance(seconds=RETRY_DELAY)
    rig.tick()
    assert rig.shows == 1 and rig.shown_id() == "alpha"

    rig.db.list_photos = real  # type: ignore[method-assign]
    rig.run_until(rig.clock.now + timedelta(minutes=3, seconds=1))
    assert rig.source()["type"] == "photo"  # the photos slot works again


def test_unknown_app_in_rotation_is_skipped(make_rig) -> None:
    rig = make_rig({"rotation": [slot("ghost", 1), slot("alpha", 1)]})
    rig.tick()
    assert rig.shown_id() == "alpha"


# --- R5 + R3 lives in test_quiet_hours.py; keep-alive while paused ----------------------------


def test_keepalive_while_paused(make_rig) -> None:
    rig = make_rig({"panel": {"keepalive_hours": 1}})
    rig.tick()
    before = rig.snapshot()["rotation"]["current"]
    bin_before = (rig.data / "last_frame.bin").read_bytes()
    rig.sched.pause()
    assert rig.store.get().paused is True
    keepalive_at = rig.snapshot()["rotation"]["keepalive_at"]
    assert keepalive_at == iso(START + timedelta(hours=1))

    refreshes = rig.run_until(START + timedelta(minutes=59))
    assert refreshes == []  # paused: no rotation
    rig.clock.set(START + timedelta(hours=1))
    rig.tick()
    assert rig.shows == 2
    assert rig.source() == {"type": "keepalive", "id": "alpha", "label": "Alpha"}
    assert (rig.data / "last_frame.bin").read_bytes() == bin_before
    assert rig.snapshot()["rotation"]["current"] == before  # did not advance
    assert sum(len(a.renders) for a in rig.apps.values()) == 1  # nothing re-rendered
    assert rig.snapshot()["rotation"]["keepalive_at"] == iso(START + timedelta(hours=2))

    rig.sched.resume()
    rig.clock.advance(seconds=180)
    rig.tick()
    assert rig.shown_id() == "beta"


def test_keepalive_is_on_time_when_a_static_repeat_is_due_with_it(make_rig) -> None:
    """A rotation change that re-shows the photo already on the panel does not
    refresh it, so it cannot double as a due keep-alive: the same tick
    re-shows the last frame (it used to wait for another tick, half a second
    later per cycle)."""
    rig = make_rig({"panel": {"keepalive_hours": 1}, "rotation": [slot("photos", 10)]})
    rig.add_photo("only.jpg", "2026-10-01T10:00:00.000+00:00")
    refreshes = rig.run_until(START + timedelta(hours=1, minutes=5))
    assert refreshes == [START, START + timedelta(hours=1)]
    assert rig.source()["type"] == "keepalive"
    assert rig.snapshot()["rotation"]["current"]["started_at"] == iso(START + timedelta(hours=1))


def test_keepalive_waits_for_the_floor_after_a_failed_attempt(make_rig) -> None:
    rig = make_rig({"panel": {"keepalive_hours": 1}})
    rig.tick()  # alpha at 12:00
    rig.sched.pause()
    rig.clock.set(START + timedelta(minutes=59))
    rig.display.fail_show = True
    rig.sched.show_now(app_id="beta")
    rig.tick()  # fails at 12:59: an attempt, not a refresh
    assert rig.shows == 1
    rig.display.fail_show = False
    rig.clock.set(START + timedelta(hours=1))
    assert rig.tick() == 120  # keep-alive due since 13:00, but the floor runs to 13:02
    assert rig.shows == 1
    rig.clock.set(START + timedelta(hours=1, minutes=2))
    rig.tick()
    assert rig.shows == 2
    assert rig.source() == {"type": "keepalive", "id": "alpha", "label": "Alpha"}


def test_keepalive_without_a_frame_renders_the_current_slot(make_rig) -> None:
    rig = make_rig({"panel": {"keepalive_hours": 2}, "paused": True})
    assert rig.tick() == MAX_DELAY
    assert rig.snapshot()["rotation"]["keepalive_at"] == iso(START + timedelta(hours=2))
    rig.run_until(START + timedelta(hours=2))
    rig.tick()
    assert rig.shows == 1 and rig.shown_id() == "alpha"


def test_keepalive_without_a_frame_shows_another_photo_if_the_current_one_is_gone(
    make_rig,
) -> None:
    """No frame to re-show and the photo on the panel was deleted: the
    keep-alive shows the photos slot's next photo, not a "no photos" card
    while photos exist."""
    rig = make_rig({"panel": {"keepalive_hours": 1}, "rotation": [slot("photos", 10)]})
    a = rig.add_photo("a.jpg", "2026-10-01T10:00:00.000+00:00")
    b = rig.add_photo("b.jpg", "2026-10-02T10:00:00.000+00:00")
    rig.tick()
    shown = rig.shown_id()
    other = b if shown == a.id else a
    rig.sched.pause()
    rig.db.delete_photo(shown)
    for name in ("last_frame.bin", "last_frame.png"):
        (rig.data / name).unlink()
    rig.sched = rig.new_scheduler()  # restart: no frame in memory or on disk
    rig.clock.set(START + timedelta(hours=1))
    rig.tick()
    assert rig.shows == 2
    assert rig.source() == {"type": "photo", "id": other.id, "label": other.original_name}
    assert rig.apps["photos"].renders == [shown, other.id]
    assert rig.sched.current_photo_id() == other.id
    assert rig.snapshot()["rotation"]["current"]["started_at"] == iso(START)  # not advanced


def test_orphan_frame_without_refresh_record_is_kept_alive(make_rig) -> None:
    rig = make_rig({"paused": True})
    frame = np.full((400, 600), 4, dtype=np.uint8)
    (rig.data / "last_frame.bin").write_bytes(pack_frame(frame, 90))
    rig.sched = rig.new_scheduler()  # a fresh state finds the frame on disk
    rig.tick()
    assert rig.shows == 1
    assert (rig.display.last_frame == frame).all()
    assert rig.source()["type"] == "keepalive"


# --- rotation: empty, disabled, edited ------------------------------------------------------


def test_all_slots_disabled_keepalive_only(make_rig) -> None:
    rig = make_rig({"panel": {"keepalive_hours": 3}})
    rig.tick()
    rig.store.patch(
        {"rotation": [slot("alpha", 1, False), slot("beta", 1, False), slot("gamma", 1, False)]}
    )
    rot = rig.snapshot()["rotation"]
    assert rot["current"] is None and rot["next"] is None and rot["next_at"] is None
    assert rot["keepalive_at"] == iso(START + timedelta(hours=3))
    refreshes = rig.run_until(START + timedelta(hours=3, minutes=1))
    assert refreshes == [START + timedelta(hours=3)]
    assert rig.source()["type"] == "keepalive"
    assert [len(a.renders) for a in rig.apps.values()] == [0, 1, 0, 0]


def test_empty_rotation_waits_for_keepalive(make_rig) -> None:
    rig = make_rig({"rotation": []})
    assert rig.tick() == MAX_DELAY
    assert rig.shows == 0


def test_disabling_the_current_slot_moves_on(make_rig) -> None:
    rig = make_rig({"rotation": [slot("alpha", 30), slot("beta", 30), slot("gamma", 30)]})
    rig.tick()
    rig.store.patch({"rotation": [slot("alpha", 30, False), slot("beta", 30), slot("gamma", 30)]})
    rig.clock.advance(seconds=180)
    rig.tick()
    assert rig.shown_id() == "beta"


def test_rotation_edit_follows_the_current_slot(make_rig) -> None:
    rig = make_rig({"rotation": [slot("alpha", 30), slot("beta", 30)]})
    rig.tick()
    started = rig.snapshot()["rotation"]["current"]["started_at"]
    rig.store.patch({"rotation": [slot("gamma", 30), slot("alpha", 30), slot("beta", 30)]})
    rig.clock.advance(seconds=200)
    assert rig.tick() > 1  # alpha's 30 minutes are not over: no refresh
    current = rig.snapshot()["rotation"]["current"]
    assert current["app_id"] == "alpha" and current["started_at"] == started
    assert rig.sched._state.slot_index == 1


def test_removed_slot_moves_on_at_the_floor(make_rig) -> None:
    rig = make_rig({"rotation": [slot("alpha", 30), slot("beta", 30)]})
    rig.tick()
    rig.store.patch({"rotation": [slot("beta", 30), slot("gamma", 30)]})
    rig.clock.advance(seconds=10)
    assert rig.tick() == 170
    rig.clock.advance(seconds=170)
    rig.tick()
    assert rig.shown_id() == "beta"


# --- photos ----------------------------------------------------------------------------------

PHOTOS_10 = {
    "rotation": [
        slot("photos", 10, dwell_minutes=3, order="shuffle"),
        slot("alpha", 3),
    ]
}


def test_photos_dwell_and_slot_duration(make_rig) -> None:
    rig = make_rig(PHOTOS_10)
    p1 = rig.add_photo("one.jpg", "2026-10-01T10:00:00.000+00:00")
    p2 = rig.add_photo("two.jpg", "2026-10-02T10:00:00.000+00:00")

    rig.tick()
    first = rig.shown_id()
    assert first in (p1.id, p2.id) and rig.source()["type"] == "photo"
    second = {p1.id, p2.id} - {first}

    rig.clock.set(START + timedelta(minutes=3))
    rig.tick()
    assert {rig.shown_id()} == second  # a new photo every dwell
    rig.clock.set(START + timedelta(minutes=6))
    rig.tick()
    assert rig.shown_id() == first
    assert rig.shows == 3

    # a change at 9 min could not be replaced before 12 min, past the slot's end
    rig.clock.set(START + timedelta(minutes=9))
    assert rig.tick() == 60
    assert rig.shows == 3

    rig.clock.set(START + timedelta(minutes=10))
    rig.tick()
    assert rig.shown_id() == "alpha" and rig.shows == 4
    assert rig.snapshot()["rotation"]["current"]["app_id"] == "alpha"

    # and back to photos after alpha's 3 minutes
    rig.clock.set(START + timedelta(minutes=13))
    rig.tick()
    assert rig.source()["type"] == "photo"


def test_photos_status_reports_current_photo_and_next(make_rig) -> None:
    rig = make_rig(PHOTOS_10)
    for i in range(3):
        rig.add_photo(f"p{i}.jpg", f"2026-10-0{i + 1}T10:00:00.000+00:00")
    rig.tick()
    rot = rig.snapshot()["rotation"]
    shown = rig.shown_id()
    assert rot["current"]["app_id"] == "photos" and rot["current"]["photo_id"] == shown
    assert rot["current"]["ends_at"] == iso(START + timedelta(minutes=10))
    announced = rot["next"]
    assert announced["app_id"] == "photos" and announced["photo_id"] not in (None, shown)
    assert rot["next_at"] == iso(START + timedelta(minutes=3))
    rig.clock.set(START + timedelta(minutes=3))
    rig.tick()
    assert rig.shown_id() == announced["photo_id"]  # the shuffle pick was reserved


def test_no_photos_card_lasts_one_dwell(make_rig) -> None:
    rig = make_rig(PHOTOS_10)
    rig.tick()
    assert rig.source() == {"type": "app", "id": "photos", "label": "Photos"}
    assert rig.apps["photos"].renders == [None]
    rig.clock.set(START + timedelta(minutes=3))
    rig.tick()
    assert rig.shown_id() == "alpha"


def test_single_photo_is_not_refreshed_needlessly(make_rig) -> None:
    rig = make_rig({"rotation": [slot("photos", 10, dwell_minutes=3)]})
    rig.add_photo("only.jpg", "2026-10-01T10:00:00.000+00:00")
    refreshes = rig.run_until(START + timedelta(hours=2))
    assert refreshes == [START]  # same photo, same panel: no pointless flicker
    assert rig.snapshot()["rotation"]["current"]["app_id"] == "photos"


def test_invalidate_repaints_the_same_photo_at_the_next_change(make_rig) -> None:
    """``invalidate(photo_id)`` (Phase 3: the photo's recipe changed) makes the
    next rotation change re-render the photo on the panel instead of skipping
    it as "already showing".  It survives a restart, a keep-alive re-show of
    the old frame in between does not clear it, and it applies only once."""
    rig = make_rig(
        {"panel": {"keepalive_hours": 1}, "rotation": [slot("photos", 120, dwell_minutes=3)]}
    )
    photo = rig.add_photo("only.jpg", "2026-10-01T10:00:00.000+00:00")
    rig.tick()  # 12:00
    assert rig.sched.invalidate("not-on-the-panel") is False
    assert rig.sched.invalidate(photo.id) is True
    rig.sched = rig.new_scheduler()  # restart
    refreshes = rig.run_until(START + timedelta(hours=4, minutes=30))
    hours = [START + timedelta(hours=h) for h in (1, 2, 3, 4)]
    assert refreshes == hours  # keep-alives; the slot's visits at 14:00 and 16:00
    assert rig.apps["photos"].renders == [photo.id, photo.id]  # re-rendered once, at 14:00
    assert rig.sched._state.panel_stale is False


def test_failed_refresh_of_static_content_is_repaired_at_the_next_change(make_rig) -> None:
    """After a failed attempt the panel's content is unknown, so the same
    photo is not skipped as "already showing" (the probe in
    test_scheduler_probe.py shows the white panel this left)."""
    rig = make_rig(
        {"panel": {"keepalive_hours": 1}, "rotation": [slot("photos", 10, dwell_minutes=3)]}
    )
    photo = rig.add_photo("only.jpg", "2026-10-01T10:00:00.000+00:00")
    rig.tick()  # 12:00
    rig.clock.set(START + timedelta(minutes=5))
    rig.display.fail_show = True
    rig.sched.show_now(photo_id=photo.id)
    rig.tick()  # 12:05: fails
    rig.display.fail_show = False
    assert rig.sched._state.panel_stale is True
    refreshes = rig.run_until(START + timedelta(minutes=45))
    assert refreshes == [START + timedelta(minutes=10)]
    assert rig.source()["type"] == "photo" and rig.sched._state.panel_stale is False


def test_photo_uploaded_while_no_photos_card_shows(make_rig) -> None:
    rig = make_rig({"rotation": [slot("photos", 10, dwell_minutes=3)]})
    rig.tick()
    photo = rig.add_photo("new.jpg", "2026-10-01T10:00:00.000+00:00")
    rig.clock.set(START + timedelta(minutes=3))
    rig.tick()
    assert rig.shown_id() == photo.id


def test_sequential_order_cycles_in_created_at_order(make_rig) -> None:
    rig = make_rig({"rotation": [slot("photos", 60, dwell_minutes=3, order="sequential")]})
    c = rig.add_photo("c.jpg", "2026-10-03T10:00:00.000+00:00")
    a = rig.add_photo("a.jpg", "2026-10-01T10:00:00.000+00:00")
    b = rig.add_photo("b.jpg", "2026-10-02T10:00:00.000+00:00")
    shown = []
    for minute in range(0, 18, 3):
        rig.clock.set(START + timedelta(minutes=minute))
        rig.tick()
        shown.append(rig.shown_id())
    assert shown == [a.id, b.id, c.id, a.id, b.id, c.id]


def test_sequential_order_skips_excluded_and_survives_deletion(make_rig) -> None:
    rig = make_rig({"rotation": [slot("photos", 60, dwell_minutes=3, order="sequential")]})
    a = rig.add_photo("a.jpg", "2026-10-01T10:00:00.000+00:00")
    b = rig.add_photo("b.jpg", "2026-10-02T10:00:00.000+00:00")
    c = rig.add_photo("c.jpg", "2026-10-03T10:00:00.000+00:00")
    d = rig.add_photo("d.jpg", "2026-10-04T10:00:00.000+00:00")
    rig.db.set_photo_excluded(c.id, True)
    rig.tick()
    assert rig.shown_id() == a.id
    rig.clock.advance(minutes=3)
    rig.tick()
    assert rig.shown_id() == b.id
    rig.db.delete_photo(b.id)  # the anchor vanishes: the walk continues after it
    rig.clock.advance(minutes=3)
    rig.tick()
    assert rig.shown_id() == d.id


def test_shuffle_avoids_immediate_repeats(make_rig) -> None:
    rig = make_rig({"rotation": [slot("photos", 600, dwell_minutes=3)]})
    ids = {rig.add_photo(f"p{i}.jpg", f"2026-10-0{i + 1}T10:00:00.000+00:00").id for i in range(4)}
    shown = []
    for k in range(40):
        rig.clock.set(START + timedelta(minutes=3 * k))
        rig.tick()
        shown.append(rig.shown_id())
    assert set(shown) == ids
    # with 4 photos the last 3 are avoided: every window of 4 shows each once
    for i in range(len(shown) - 3):
        assert len(set(shown[i : i + 4])) == 4


def _photos(n: int) -> list[Photo]:
    return [
        Photo(
            id=f"{i:032x}",
            original_name=f"p{i}.jpg",
            created_at=f"2026-10-01T10:{i:02d}:00.000+00:00",
            width=600,
            height=400,
            portrait=False,
            excluded=False,
            recipe=None,
            master_path="m",
            master_bytes=1,
        )
        for i in range(n)
    ]


def test_pick_next_photo_rules() -> None:
    rng = random.Random(3)
    assert pick_next_photo([], "shuffle", [], rng) is None
    one = _photos(1)
    assert pick_next_photo(one, "shuffle", [one[0].id], rng) is one[0]
    photos = _photos(25)
    recent = [p.id for p in photos[:20]]
    for _ in range(50):
        assert pick_next_photo(photos, "shuffle", recent, rng).id not in recent
    two = _photos(2)
    assert pick_next_photo(two, "shuffle", [two[0].id], rng) is two[1]
    assert pick_next_photo(two, "sequential", [], rng) is two[0]
    assert pick_next_photo(two, "sequential", [two[1].id], rng) is two[0]  # cycles
    # the current photo is never picked while another exists
    assert pick_next_photo(two, "sequential", [two[1].id], rng, current=two[0].id) is two[1]
    assert pick_next_photo(two, "shuffle", [], rng, current=two[0].id) is two[1]


def test_validate_photo_options() -> None:
    assert validate_photo_options({}) == {"dwell_minutes": 3, "order": "shuffle"}
    assert validate_photo_options({"dwell_minutes": 5.0, "order": "sequential"}) == {
        "dwell_minutes": 5,
        "order": "sequential",
    }
    for bad, fragment in (
        ({"dwell_minutes": 0}, "between 1 and 1440"),
        ({"dwell_minutes": 1441}, "between 1 and 1440"),
        ({"dwell_minutes": 2.5}, "whole number"),
        ({"dwell_minutes": "3"}, "whole number"),
        ({"dwell_minutes": True}, "whole number"),
        ({"order": "random"}, "options.order"),
        ({"speed": 1}, "unknown key"),
        ([1], "object"),
    ):
        with pytest.raises(ValueError, match=fragment):
            validate_photo_options(bad)


def test_broken_photo_is_skipped_and_walked_past(make_rig) -> None:
    apps = fake_apps()
    rig = make_rig(
        {
            "panel": {"refresh_floor_seconds": 30},
            "rotation": [slot("photos", 60, dwell_minutes=3, order="sequential"), slot("alpha", 3)],
        },
        apps=apps,
    )
    a = rig.add_photo("a.jpg", "2026-10-01T10:00:00.000+00:00")
    b = rig.add_photo("b.jpg", "2026-10-02T10:00:00.000+00:00")
    c = rig.add_photo("c.jpg", "2026-10-03T10:00:00.000+00:00")
    real_render = apps["photos"].render

    def render(ctx):
        if ctx.item_id == b.id:
            raise ValueError("corrupt master")
        return real_render(ctx)

    apps["photos"].render = render  # type: ignore[method-assign]
    rig.tick()
    assert rig.shown_id() == a.id
    rig.clock.advance(minutes=3)
    assert rig.tick() == RETRY_DELAY  # b fails: the slot is skipped
    rig.clock.advance(seconds=1)
    rig.tick()
    assert rig.shown_id() == "alpha"
    rig.clock.advance(minutes=3)
    rig.tick()
    assert rig.shown_id() == c.id  # the walk moved past the broken photo


# --- R7: persistence ---------------------------------------------------------------------


def test_state_survives_restart(make_rig) -> None:
    rig = make_rig(
        {
            "panel": {"refresh_floor_seconds": 30, "keepalive_hours": 5},
            "rotation": [slot("alpha", 1), slot("photos", 10, dwell_minutes=3), slot("beta", 1)],
        }
    )
    rig.add_photo("one.jpg", "2026-10-01T10:00:00.000+00:00")
    rig.add_photo("two.jpg", "2026-10-02T10:00:00.000+00:00")
    rig.tick()  # alpha
    rig.clock.set(START + timedelta(minutes=1))
    rig.tick()  # photos, first photo
    rig.clock.set(START + timedelta(minutes=4))
    rig.tick()  # photos, second photo
    before = rig.snapshot()
    state_before = rig.sched._state

    # "restart": new display object, new scheduler, same db and data dir
    rig.display = RecordingDisplay(rig.data)
    rig.clock.advance(seconds=20)
    rig.sched = rig.new_scheduler()
    after = rig.snapshot()
    st = rig.sched._state
    assert st.slot_index == state_before.slot_index == 1
    assert st.photo_id == state_before.photo_id and st.photo_id is not None
    assert st.recent_photo_ids == state_before.recent_photo_ids
    for key in ("last_refresh_at", "last_attempt_at", "last_source"):
        assert after["panel"][key] == before["panel"][key]
    assert after["panel"]["last_refresh_at"] == iso(START + timedelta(minutes=4))
    assert after["rotation"]["current"] == before["rotation"]["current"]
    assert after["rotation"]["keepalive_at"] == iso(START + timedelta(hours=5, minutes=4))
    # the photo's dwell carries over: the next photo is still due at 12:07
    assert rig.tick() == pytest.approx(160)
    # and so does the last attempt: a show-now waits 30 s from 12:04, not from now
    info = rig.sched.show_now(app_id="beta")
    assert info["fires_at"] == iso(START + timedelta(minutes=4, seconds=30))
    assert rig.tick() == pytest.approx(10)
    assert rig.display.show_count == 0

    # restart again; the keep-alive re-shows the right frame, read back from disk
    rig.sched = rig.new_scheduler()
    rig.sched.pause()
    bin_before = (rig.data / "last_frame.bin").read_bytes()
    rig.clock.set(START + timedelta(hours=5, minutes=4))
    rig.tick()
    assert rig.display.show_count == 1 and rig.source()["type"] == "keepalive"
    assert (rig.data / "last_frame.bin").read_bytes() == bin_before
    assert rig.snapshot()["rotation"]["current"] == before["rotation"]["current"]


def test_restart_clamps_slot_index_and_drops_deleted_photo(make_rig) -> None:
    rig = make_rig({"rotation": [slot("alpha", 1), slot("beta", 1), slot("photos", 10)]})
    photo = rig.add_photo("gone.jpg", "2026-10-01T10:00:00.000+00:00")
    blob = SchedulerState(slot_index=7, slot_app_id="photos", photo_id=photo.id).to_json()
    rig.db.set_state(STATE_KEY, blob)
    rig.db.delete_photo(photo.id)
    rig.sched = rig.new_scheduler()
    st = rig.sched._state
    assert st.slot_index == 2 and st.photo_id is None


def test_corrupt_state_falls_back_to_defaults(make_rig) -> None:
    rig = make_rig()
    rig.db.set_state(STATE_KEY, ["not", "a", "dict"])
    rig.sched = rig.new_scheduler()
    rig.tick()
    assert rig.shown_id() == "alpha"
    rig.db.set_state(STATE_KEY, {"slot_index": "x", "last_attempt_at": "yesterday", "junk": 1})
    rig.sched = rig.new_scheduler()
    assert rig.sched._state.slot_index == 0
    # the unparsable attempt anchor falls back to the last refresh (its
    # mirror key), so the floor still holds (R1)
    assert rig.sched._state.last_attempt_at == START
    assert rig.tick() == pytest.approx(180)
    assert rig.shows == 1


def test_phase1_state_keys_are_picked_up(make_rig) -> None:
    rig = make_rig()
    rig.db.set_state("last_refresh_at", iso(START - timedelta(hours=1)))
    rig.db.set_state("last_source", {"type": "app", "id": "beta", "label": "Beta"})
    rig.sched = rig.new_scheduler()
    snap = rig.snapshot()
    assert snap["panel"]["last_refresh_at"] == iso(START - timedelta(hours=1))
    assert snap["panel"]["last_source"]["id"] == "beta"


# --- clock safety -----------------------------------------------------------------------


def test_wall_clock_jump_does_not_break_the_floor_or_keepalive(make_rig) -> None:
    rig = make_rig({"panel": {"keepalive_hours": 1}})
    mono = {"t": 0.0}
    rig.sched = rig.new_scheduler(monotonic_fn=lambda: mono["t"])
    rig.tick()
    # NTP steps the clock 5 h forward while only 60 s really passed
    rig.clock.advance(hours=5, seconds=60)
    mono["t"] += 60
    rig.sched.pause()  # so only a (wrong) keep-alive could refresh
    assert rig.tick() >= 100  # neither the floor nor the keep-alive is fooled
    assert rig.shows == 1
    snap = rig.snapshot()
    assert snap["rotation"]["keepalive_at"] == iso(rig.clock.now + timedelta(minutes=59))


def test_show_now_queued_across_a_clock_jump_still_fires(make_rig) -> None:
    rig = make_rig()
    mono = {"t": 0.0}
    rig.sched = rig.new_scheduler(monotonic_fn=lambda: mono["t"])
    rig.tick()
    rig.clock.advance(hours=5, seconds=60)  # the jump happens between two ticks ...
    mono["t"] += 60
    rig.sched.show_now(app_id="gamma")  # ... and a request arrives before the next one
    rig.tick()
    assert rig.shows == 2 and rig.shown_id() == "gamma"


def test_restart_with_clock_behind_clamps_future_anchors(make_rig) -> None:
    rig = make_rig()
    rig.tick()
    # restart with the clock two hours behind (no RTC, NTP not synced yet)
    rig.clock = FakeClock(START - timedelta(hours=2))
    rig.sched = rig.new_scheduler()
    st = rig.sched._state
    assert st.last_attempt_at == rig.clock.now and st.last_refresh_at == rig.clock.now
    assert rig.tick() == 180  # a whole floor from "now": safe, never stuck


# --- the thread ----------------------------------------------------------------------------


class CountingScheduler(Scheduler):
    """A Scheduler that counts its ticks."""

    ticks = 0

    def tick(self, now):  # type: ignore[override]
        self.ticks += 1
        return super().tick(now)


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def real_clock(tmp_path: Path):
    """``build(display) -> CountingScheduler`` on the real clocks; closes the db."""
    dbs: list[Database] = []

    def build(display: RecordingDisplay) -> CountingScheduler:
        paths = DataPaths(tmp_path / "data").ensure_dirs()
        store = SettingsStore(paths.settings_file)
        store.patch({"quiet_hours": {"enabled": False}, "rotation": [slot("alpha", 1)]})
        db = Database(paths.db_file)
        dbs.append(db)
        return CountingScheduler(display, store, db, paths, {"alpha": FakeApp("alpha", 2)})

    yield build
    for db in dbs:
        db.close()


def test_thread_start_wake_stop(tmp_path: Path, real_clock) -> None:
    display = RecordingDisplay(tmp_path / "data", refresh_seconds=0)
    sched = real_clock(display)
    sched.start()
    try:
        assert _wait_for(lambda: display.show_count == 1)
        assert sched.running
        thread = next(t for t in threading.enumerate() if t.name == "scheduler")
        assert thread.daemon
        sched.start()  # idempotent
        assert sum(t.name == "scheduler" for t in threading.enumerate()) == 1
        # it now sleeps for the floor; wake() makes it look again at once
        assert _wait_for(lambda: sched.ticks >= 1)
        time.sleep(0.05)
        n = sched.ticks
        sched.wake()
        assert _wait_for(lambda: sched.ticks > n, timeout=5)
        assert display.show_count == 1  # waking never bypasses the floor
    finally:
        assert sched.stop(timeout=10) is True
    assert not sched.running
    assert not any(t.name == "scheduler" for t in threading.enumerate())
    assert display.awake is False


def test_stop_lets_a_refresh_in_flight_finish(tmp_path: Path, real_clock) -> None:
    display = RecordingDisplay(tmp_path / "data", refresh_seconds=0.4)
    sched = real_clock(display)
    sched.start()
    assert _wait_for(lambda: display.state == "refreshing")
    assert sched.stop(timeout=10) is True
    assert display.show_count == 1
    assert display.calls == ["init", "show", "sleep"]
    assert display.awake is False


def test_stop_does_not_wait_for_a_slow_render(tmp_path: Path) -> None:
    """``stop()`` is not held up by a render in flight (only a panel refresh
    is waited for): the render is abandoned without counting as a failure,
    so the rotation does not move and nothing touches the panel."""
    from concurrent.futures import ThreadPoolExecutor

    paths = DataPaths(tmp_path / "data").ensure_dirs()
    store = SettingsStore(paths.settings_file)
    store.patch({"quiet_hours": {"enabled": False}, "rotation": [slot("alpha", 1)]})
    db = Database(paths.db_file)
    gate, entered = threading.Event(), threading.Event()
    alpha = FakeApp("alpha", 2)
    real = alpha.render

    def slow(ctx):
        entered.set()
        gate.wait(20)
        return real(ctx)

    alpha.render = slow  # type: ignore[method-assign]
    display = RecordingDisplay(paths.root, refresh_seconds=0)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render")
    sched = Scheduler(display, store, db, paths, {"alpha": alpha}, executor=executor)
    sched.start()
    try:
        assert entered.wait(10)
        started = time.monotonic()
        assert sched.stop(timeout=10) is True
        assert time.monotonic() - started < 3
        assert not sched.refreshing
        snap = sched.snapshot()
        assert snap["panel"]["last_error"] is None
        assert sched._state.slot_index == 0 and sched._state.slot_started_at is None
    finally:
        gate.set()
        executor.shutdown(wait=True)
        db.close()
    assert display.calls == []


def test_tick_after_stop_does_nothing(make_rig) -> None:
    rig = make_rig()
    rig.sched.stop()
    assert rig.tick() == 0.5
    assert rig.shows == 0
