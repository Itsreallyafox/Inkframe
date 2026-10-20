"""Quiet hours (R5): the window maths, and what the scheduler does inside one.

19 October 2026 is a Monday (weekday 0); the 25th is a Sunday (6).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from inkframe.config import QuietHours
from inkframe.scheduler import MAX_DELAY, iso, quiet_window_active
from tests.conftest import TZ, FakeClock, at, slot

ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)
NIGHT = QuietHours(enabled=True, start="23:00", end="07:00", days=ALL_DAYS)


# --- the window function ------------------------------------------------------------


@pytest.mark.parametrize(
    ("when", "active", "until"),
    [
        (at(19, 22, 59, 59), False, None),
        (at(19, 23, 0, 0), True, at(20, 7)),  # the start is inside
        (at(19, 23, 30), True, at(20, 7)),
        (at(20, 0, 0), True, at(20, 7)),  # past midnight: the window that began yesterday
        (at(20, 6, 59, 59), True, at(20, 7)),
        (at(20, 7, 0, 0), False, None),  # the end is outside
        (at(20, 12, 0), False, None),
    ],
)
def test_overnight_window(when: datetime, active: bool, until: datetime | None) -> None:
    assert quiet_window_active(NIGHT, when) == (active, until)


def test_window_belongs_to_the_day_it_starts() -> None:
    sunday_night = QuietHours(enabled=True, start="23:00", end="07:00", days=(6,))
    assert quiet_window_active(sunday_night, at(25, 23, 30)) == (True, at(26, 7))
    # Monday 02:00 is inside the window that started on Sunday
    assert quiet_window_active(sunday_night, at(26, 2, 0)) == (True, at(26, 7))
    # Monday night's window would start on Monday, which is not listed
    assert quiet_window_active(sunday_night, at(19, 23, 30)) == (False, None)
    assert quiet_window_active(sunday_night, at(20, 2, 0)) == (False, None)
    # Saturday 02:00 belongs to Friday night (not listed)
    assert quiet_window_active(sunday_night, at(24, 2, 0)) == (False, None)


def test_daytime_window() -> None:
    lunch = QuietHours(enabled=True, start="13:00", end="14:00", days=(0,))
    assert quiet_window_active(lunch, at(19, 13, 30)) == (True, at(19, 14))
    assert quiet_window_active(lunch, at(19, 14, 0)) == (False, None)
    assert quiet_window_active(lunch, at(19, 12, 59)) == (False, None)
    assert quiet_window_active(lunch, at(20, 13, 30)) == (False, None)  # Tuesday


def test_disabled_or_dayless_quiet_hours_never_apply() -> None:
    off = QuietHours(enabled=False, start="23:00", end="07:00", days=ALL_DAYS)
    no_days = QuietHours(enabled=True, start="23:00", end="07:00", days=())
    for when in (at(19, 23, 30), at(20, 2, 0), at(20, 12, 0)):
        assert quiet_window_active(off, when) == (False, None)
        assert quiet_window_active(no_days, when) == (False, None)


def test_window_uses_the_wall_clock_of_nows_zone() -> None:
    # 01:30 UTC is quiet on a UTC wall clock, but it is 21:30 the evening
    # before in UTC-4, which is not
    utc = datetime(2026, 10, 20, 1, 30, tzinfo=timezone.utc)
    assert quiet_window_active(NIGHT, utc) == (True, datetime(2026, 10, 20, 7, tzinfo=timezone.utc))
    assert quiet_window_active(NIGHT, utc.astimezone(TZ)) == (False, None)
    # 03:30 UTC is 23:30 in UTC-4: quiet until 07:00 local, i.e. 11:00 UTC
    active, until = quiet_window_active(
        NIGHT, datetime(2026, 10, 20, 3, 30, tzinfo=timezone.utc).astimezone(TZ)
    )
    assert active and until == at(20, 7) == datetime(2026, 10, 20, 11, tzinfo=timezone.utc)


def test_window_across_a_dst_change() -> None:
    # local_now() gives a fixed offset, which changes when the clocks go back
    # (1 November 2026, 02:00 EDT -> 01:00 EST)
    edt, est = timezone(timedelta(hours=-4)), timezone(timedelta(hours=-5))
    t = datetime(2026, 11, 1, 0, 30, tzinfo=edt)
    active, until = quiet_window_active(NIGHT, t)
    # "until" is computed on the 00:30 wall clock: 07:00 EDT, an hour early in real time
    assert active and until == datetime(2026, 11, 1, 7, 0, tzinfo=edt)
    # at that instant the system clock reads 06:00 EST: still quiet, until 07:00 local
    later = until.astimezone(est)
    assert later.hour == 6
    assert quiet_window_active(NIGHT, later) == (True, datetime(2026, 11, 1, 7, 0, tzinfo=est))
    assert quiet_window_active(NIGHT, datetime(2026, 11, 1, 7, 0, tzinfo=est)) == (False, None)


# --- the scheduler inside quiet hours --------------------------------------------------

QUIET = {
    "panel": {"refresh_floor_seconds": 30, "show_now_min_seconds": 30, "keepalive_hours": 20},
    "quiet_hours": {"enabled": True, "start": "23:00", "end": "07:00", "days": list(ALL_DAYS)},
    "rotation": [slot("alpha", 1), slot("beta", 1), slot("gamma", 1)],
}


def test_quiet_hours_boundary(make_rig) -> None:
    rig = make_rig(QUIET, clock=FakeClock(at(19, 22, 59)))
    rig.tick()
    assert rig.shows == 1  # 22:59: a normal refresh

    rig.clock.set(at(19, 23, 0, 30))  # floor and slot both allow one: quiet forbids it
    assert rig.tick() == MAX_DELAY
    assert rig.shows == 1
    rot = rig.snapshot()["rotation"]
    assert rot["quiet"] is True and rot["quiet_until"] == iso(at(20, 7))
    assert rot["next_at"] == iso(at(20, 7))

    refreshes = rig.run_until(at(20, 7))
    assert refreshes == []  # the whole night: nothing
    rig.clock.set(at(20, 7, 0, 30))
    rig.tick()  # 07:00:30: the rotation resumes
    assert rig.shows == 2
    assert rig.snapshot()["rotation"]["quiet"] is False


def test_quiet_hours_resume_on_time(make_rig) -> None:
    rig = make_rig(QUIET, clock=FakeClock(at(19, 22, 59)))
    refreshes = rig.run_until(at(20, 7, 5))
    night = [t for t in refreshes if at(19, 23) <= t < at(20, 7)]
    assert night == []
    assert refreshes[-1] >= at(20, 7) and any(at(20, 7) <= t < at(20, 7, 1) for t in refreshes)


def test_window_whose_start_day_is_not_listed_does_not_apply(make_rig) -> None:
    rig = make_rig(
        {**QUIET, "quiet_hours": {**QUIET["quiet_hours"], "days": [6]}},  # Sunday nights only
        clock=FakeClock(at(19, 23, 30)),  # Monday night
    )
    rig.tick()
    assert rig.shows == 1
    rig.clock.set(at(20, 2, 0))  # Tuesday 02:00, inside Monday's (unlisted) window
    rig.tick()
    assert rig.shows == 2


def test_disabled_quiet_hours_never_apply(make_rig) -> None:
    rig = make_rig(
        {**QUIET, "quiet_hours": {**QUIET["quiet_hours"], "enabled": False}},
        clock=FakeClock(at(19, 23, 30)),
    )
    refreshes = rig.run_until(at(20, 0, 30))
    assert len(refreshes) >= 55  # one a minute, right through the night
    assert rig.snapshot()["rotation"]["quiet"] is False


def test_keepalive_during_quiet_hours_reshows_last_frame(make_rig) -> None:
    settings = {
        **QUIET,
        "panel": {**QUIET["panel"], "keepalive_hours": 2},
        "rotation": [slot("alpha", 10), slot("beta", 10), slot("gamma", 10)],
    }
    rig = make_rig(settings, clock=FakeClock(at(19, 22, 58)))
    rig.tick()
    assert rig.shown_id() == "alpha"
    frame = rig.display.last_frame.copy()
    bin_before = (rig.data / "last_frame.bin").read_bytes()
    current = rig.snapshot()["rotation"]["current"]
    assert rig.snapshot()["rotation"]["keepalive_at"] == iso(at(20, 0, 58))

    assert rig.run_until(at(20, 0, 58)) == []
    rig.tick()  # 00:58, deep in quiet hours: the keep-alive is due
    assert rig.shows == 2
    assert rig.source() == {"type": "keepalive", "id": "alpha", "label": "Alpha"}
    assert (rig.display.last_frame == frame).all()  # the same image ...
    assert (rig.data / "last_frame.bin").read_bytes() == bin_before  # ... byte for byte
    assert rig.snapshot()["rotation"]["current"] == current  # the rotation did not move
    assert [len(a.renders) for a in rig.apps.values()] == [0, 1, 0, 0]  # nothing rendered
    assert rig.snapshot()["rotation"]["keepalive_at"] == iso(at(20, 2, 58))

    rig.clock.set(at(20, 2, 58))
    rig.tick()
    assert rig.shows == 3 and rig.source()["type"] == "keepalive"
    rig.run_until(at(20, 7, 0, 30))
    rig.tick()
    assert rig.shown_id() == "beta"  # morning: the rotation carries on where it was


def test_show_now_during_quiet_hours(make_rig) -> None:
    rig = make_rig(QUIET, clock=FakeClock(at(19, 22, 58)))
    rig.tick()
    rig.clock.set(at(19, 23, 30))
    info = rig.sched.show_now(app_id="gamma")
    assert info["fires_at"] == iso(at(19, 23, 30))
    rig.tick()
    assert rig.shows == 2 and rig.shown_id() == "gamma"  # the owner's choice
    rig.clock.set(at(19, 23, 31))
    rig.sched.show_next()
    rig.tick()
    assert rig.shows == 3 and rig.shown_id() == "alpha"
    # and the rotation itself still sleeps
    assert rig.run_until(at(20, 6, 59)) == []


def test_quiet_hours_starting_mid_render_drop_the_frame(make_rig) -> None:
    rig = make_rig(QUIET, clock=FakeClock(at(19, 22, 57)))
    rig.tick()
    rig.clock.set(at(19, 22, 59, 59))
    real_render = rig.apps["beta"].render

    def slow_render(ctx):
        rig.clock.advance(seconds=5)  # quiet hours begin while beta renders
        return real_render(ctx)

    rig.apps["beta"].render = slow_render  # type: ignore[method-assign]
    rig.tick()
    assert rig.shows == 1
