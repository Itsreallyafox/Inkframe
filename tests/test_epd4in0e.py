"""Wire-level tests for the real-panel driver, run against fake SPI/GPIO.

Every fake records into one shared event log so ordering across SPI writes,
GPIO line changes and delays can be asserted exactly.  SPI writes are logged
as ``("cmd", b)`` or ``("data", b)`` according to the DC line at the moment of
the write, the way the controller itself decodes them.  Delays go through a
fake clock, so the whole suite runs instantly and timeouts are measured in
fake seconds.

The expected byte sequences are restated here from the vendor driver
(``vendor/waveshare/epd4in0e.py``), not imported from the module under test,
so a typo in the port fails the suite.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from inkframe.display import epd4in0e as epd
from inkframe.display.base import Display, DisplayError, DisplayNotReady, DisplayTimeout
from inkframe.display.palette import FRAME_BYTES, pack_frame

REPO_ROOT = Path(__file__).resolve().parents[1]
POLL = 0.005
Event = tuple[str, Any]

#: vendor epd4in0e.py 122-181
VENDOR_INIT: list[tuple[int, list[int]]] = [
    (0xAA, [0x49, 0x55, 0x20, 0x08, 0x09, 0x18]),
    (0x01, [0x3F]),
    (0x00, [0x5F, 0x69]),
    (0x05, [0x40, 0x1F, 0x1F, 0x2C]),
    (0x08, [0x6F, 0x1F, 0x1F, 0x22]),
    (0x06, [0x6F, 0x1F, 0x17, 0x17]),
    (0x03, [0x00, 0x54, 0x00, 0x44]),
    (0x60, [0x02, 0x00]),
    (0x30, [0x08]),
    (0x50, [0x3F]),
    (0x61, [0x01, 0x90, 0x02, 0x58]),
    (0xE3, [0x2F]),
    (0x84, [0x01]),
]


# --- fakes -------------------------------------------------------------------------


class FakeClock:
    """``sleep_fn`` + ``monotonic_fn`` pair; every sleep is logged and advances time."""

    def __init__(self, log: list[Event]) -> None:
        self.log = log
        self.now = 1000.0

    def sleep(self, seconds: float) -> None:
        self.log.append(("sleep", seconds))
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


class FakeGpio:
    """Tracks RST/DC/PWR and simulates BUSY.

    After every command byte and every RST rising edge the controller is
    "busy" for ``busy_low_polls`` reads (or ``low_polls_after[cmd]`` reads),
    then reads HIGH once, which ends the driver's wait.  ``stuck`` makes BUSY
    read LOW forever; ``stuck_after`` sets ``stuck`` when that command byte is
    written.
    """

    def __init__(self, log: list[Event], problems: list[str], busy_low_polls: int = 3) -> None:
        self.log = log
        self.problems = problems
        self.busy_low_polls = busy_low_polls
        self.low_polls_after: dict[int, int] = {}
        self.stuck = False
        self.stuck_after: int | None = None
        self.on_busy_read: Callable[[], None] | None = None
        self.fail_pwr_low = 0  # number of upcoming set_pwr(False) calls that raise
        self.is_open = False
        self.opens = 0
        self.rst = False
        self.dc = False
        self.pwr = False
        self.low_reads = 0
        self._remaining = 0

    def _check_open(self, what: str) -> None:
        if not self.is_open:
            self.problems.append(f"GPIO {what} while lines are not claimed")
            raise RuntimeError(f"GPIO {what} while lines are not claimed")

    def open(self) -> None:
        if self.is_open:
            self.problems.append("GPIO lines claimed twice")
        self.is_open = True
        self.opens += 1
        self.log.append(("gpio_open", None))

    def set_rst(self, high: bool) -> None:
        self._check_open("set_rst")
        if high and not self.rst:
            self._remaining = self.busy_low_polls  # controller busy coming out of reset
        self.rst = high
        self.log.append(("rst", high))

    def set_dc(self, high: bool) -> None:
        self._check_open("set_dc")
        self.dc = high
        self.log.append(("dc", high))

    def set_pwr(self, high: bool) -> None:
        self._check_open("set_pwr")
        if not high and self.fail_pwr_low > 0:
            self.fail_pwr_low -= 1
            raise OSError("simulated GPIO write failure")
        self.pwr = high
        self.log.append(("pwr", high))

    def notify_command(self, cmd: int) -> None:
        self._remaining = self.low_polls_after.get(cmd, self.busy_low_polls)
        if self.stuck_after is not None and cmd == self.stuck_after:
            self.stuck = True

    def read_busy(self) -> bool:
        self._check_open("read_busy")
        if self.on_busy_read is not None:
            self.on_busy_read()
        if self.stuck or self._remaining > 0:
            self._remaining = max(0, self._remaining - 1)
            self.low_reads += 1
            return False
        self.log.append(("busy_released", None))
        return True

    def close(self) -> None:
        self.is_open = False
        self.log.append(("gpio_close", None))


class FakeSpi:
    """Records writes as cmd/data from the DC line; can inject failures."""

    def __init__(self, log: list[Event], problems: list[str], gpio: FakeGpio) -> None:
        self.log = log
        self.problems = problems
        self.gpio = gpio
        self.is_open = False
        self.max_speed_hz: int | None = None
        self.mode: int | None = None
        self.fail_open: BaseException | None = None
        self.fail_writebytes: BaseException | None = None
        self.fail_writebytes2: BaseException | None = None

    def _problem(self, msg: str) -> None:
        self.problems.append(msg)
        raise RuntimeError(msg)

    def open(self, bus: int, device: int) -> None:
        if self.fail_open is not None:
            raise self.fail_open
        if self.is_open:
            self._problem("SPI opened twice")
        if not self.gpio.pwr:
            self._problem("SPI opened before panel power")
        self.is_open = True
        self.log.append(("open", (bus, device)))

    def configure(self, max_speed_hz: int, mode: int) -> None:
        if not self.is_open:
            self._problem("SPI configured while closed")
        self.max_speed_hz, self.mode = max_speed_hz, mode
        self.log.append(("configure", (max_speed_hz, mode)))

    def _check_write(self) -> None:
        if not self.is_open:
            self._problem("SPI write while closed")
        if not self.gpio.pwr:
            self._problem("SPI write with panel power off")

    def writebytes(self, data: list[int]) -> None:
        self._check_write()
        if self.fail_writebytes is not None:
            raise self.fail_writebytes
        if len(data) != 1:
            self._problem(f"writebytes with {len(data)} bytes; the vendor sends one per transfer")
        value = data[0]
        if self.gpio.dc:
            self.log.append(("data", value))
        else:
            self.log.append(("cmd", value))
            self.gpio.notify_command(value)

    def writebytes2(self, data: bytes) -> None:
        self._check_write()
        if not self.gpio.dc:
            self._problem("frame sent with DC low")
        if self.fail_writebytes2 is not None:
            raise self.fail_writebytes2
        self.log.append(("data2", bytes(data)))

    def close(self) -> None:
        if not self.is_open:
            self._problem("SPI closed twice")
        self.is_open = False
        self.log.append(("close", None))


class Rig:
    """One display wired to fakes that share a single event log."""

    def __init__(
        self,
        rotation: int = 90,
        busy_timeout_s: float = 60.0,
        short_timeout_s: float = 10.0,
        busy_low_polls: int = 3,
    ) -> None:
        self.log: list[Event] = []
        self.problems: list[str] = []
        self.rotation = rotation
        self.clock = FakeClock(self.log)
        self.gpio = FakeGpio(self.log, self.problems, busy_low_polls)
        self.spi = FakeSpi(self.log, self.problems, self.gpio)
        self.disp = epd.EPD4in0e(
            rotation_fn=lambda: self.rotation,
            busy_timeout_s=busy_timeout_s,
            spi=self.spi,
            gpio=self.gpio,
            sleep_fn=self.clock.sleep,
            monotonic_fn=self.clock.monotonic,
            short_timeout_s=short_timeout_s,
        )

    def awake(self) -> Rig:
        self.disp.init()
        self.log.clear()
        return self

    def assert_powered_down(self) -> None:
        assert not self.gpio.pwr, "PWR left high"
        assert not self.gpio.rst, "RST left high"
        assert not self.gpio.dc, "DC left high"
        assert not self.spi.is_open, "SPI left open"
        assert not self.disp.awake
        assert self.disp.state == "asleep"


@pytest.fixture
def rig() -> Iterator[Rig]:
    r = Rig()
    yield r
    assert r.problems == []


@pytest.fixture
def frame() -> np.ndarray:
    return np.random.default_rng(1234).integers(0, 6, size=(400, 600), dtype=np.uint8)


# --- log helpers ------------------------------------------------------------------------


def wire(log: list[Event]) -> list[tuple[int, list[Any]]]:
    """SPI traffic grouped as ``(command, [data bytes..., or the frame as bytes])``."""
    groups: list[tuple[int, list[Any]]] = []
    for kind, value in log:
        if kind == "cmd":
            groups.append((value, []))
        elif kind in ("data", "data2"):
            assert groups, f"{kind} before any command"
            groups[-1][1].append(value)
    return groups


def timeline(log: list[Event]) -> list[Event]:
    """The log without DC writes and BUSY poll sleeps; frames shown by length."""
    out: list[Event] = []
    for kind, value in log:
        if kind == "dc" or (kind == "sleep" and value == POLL):
            continue
        out.append((kind, len(value)) if kind == "data2" else (kind, value))
    return out


def as_events(groups: list[tuple[int, list[int]]]) -> list[Event]:
    out: list[Event] = []
    for cmd, data in groups:
        out.append(("cmd", cmd))
        out.extend(("data", b) for b in data)
    return out


def poll_seconds_between(log: list[Event], start: int, end: int) -> float:
    return sum(v for k, v in log[start:end] if k == "sleep" and v == POLL)


# one line per command with its data bytes, as in the vendor driver
# fmt: off
#: what TurnOnDisplay must put on the timeline (busy_released = end of a BUSY wait)
TURN_ON_TIMELINE: list[Event] = [
    ("cmd", 0x04),
    ("busy_released", None), ("sleep", 0.2), ("sleep", 0.2),
    ("cmd", 0x06), ("data", 0x6F), ("data", 0x1F), ("data", 0x17), ("data", 0x27),
    ("sleep", 0.2),
    ("cmd", 0x12), ("data", 0x00),
    ("busy_released", None), ("sleep", 0.2),
    ("cmd", 0x02), ("data", 0x00),
    ("busy_released", None), ("sleep", 0.2), ("sleep", 0.2),
]

SLEEP_EVENTS: list[Event] = [
    ("dc", False), ("cmd", 0x07), ("dc", True), ("data", 0xA5),
    ("sleep", 2.0),
    ("close", None),
    ("rst", False), ("dc", False), ("pwr", False),
]
# fmt: on


# --- (1) init -------------------------------------------------------------------------


def test_init_sends_exact_vendor_sequence(rig: Rig) -> None:
    rig.disp.init()

    assert wire(rig.log) == VENDOR_INIT
    # 0x61 resolution bytes are the native portrait frame, 400 x 600
    assert (0x01 << 8 | 0x90, 0x02 << 8 | 0x58) == (400, 600)
    assert rig.spi.max_speed_hz == 4_000_000 and rig.spi.mode == 0

    expected = [
        ("gpio_open", None),
        ("pwr", True),
        ("open", (0, 0)),
        ("configure", (4_000_000, 0)),
        ("rst", True),
        ("sleep", 0.02),
        ("rst", False),
        ("sleep", 0.002),
        ("rst", True),
        ("sleep", 0.02),
        ("busy_released", None),
        ("sleep", 0.2),
        ("sleep", 0.03),
        *as_events(VENDOR_INIT),
        ("busy_released", None),
        ("sleep", 0.2),
    ]
    assert timeline(rig.log) == expected
    assert rig.log.index(("pwr", True)) < rig.log.index(("open", (0, 0)))
    assert rig.gpio.low_reads == 2 * rig.gpio.busy_low_polls
    assert rig.disp.awake and rig.disp.state == "awake"


def test_dc_is_low_for_commands_and_high_for_data(rig: Rig) -> None:
    rig.disp.init()
    # every write is preceded by a DC write that matches its decoded kind
    for i, (kind, _) in enumerate(rig.log):
        if kind in ("cmd", "data"):
            assert rig.log[i - 1] == ("dc", kind == "data")


def test_reinit_while_awake_reruns_sequence_without_reopening(rig: Rig) -> None:
    rig.awake()
    rig.disp.init()
    assert wire(rig.log) == VENDOR_INIT
    assert ("open", (0, 0)) not in rig.log and ("gpio_open", None) not in rig.log
    assert rig.disp.awake


def test_init_after_sleep_reopens_bus_and_reruns_full_sequence(rig: Rig) -> None:
    rig.awake()
    rig.disp.sleep()
    rig.log.clear()
    rig.disp.init()
    assert wire(rig.log) == VENDOR_INIT
    # GPIO lines stay claimed across sleep; power and bus come back in vendor order
    assert rig.gpio.opens == 1
    assert timeline(rig.log)[:3] == [("pwr", True), ("open", (0, 0)), ("configure", (4_000_000, 0))]
    assert rig.disp.awake


# --- (2) show -------------------------------------------------------------------------


def test_show_sends_frame_then_turn_on_display(rig: Rig, frame: np.ndarray) -> None:
    rig.awake()
    rig.disp.show(frame)

    groups = wire(rig.log)
    assert [cmd for cmd, _ in groups] == [0x10, 0x04, 0x06, 0x12, 0x02]
    assert len(groups[0][1]) == 1, "frame must go out as ONE writebytes2"
    payload = groups[0][1][0]
    assert isinstance(payload, bytes) and len(payload) == FRAME_BYTES == 120000
    assert payload == pack_frame(frame, 90)

    assert timeline(rig.log) == [("cmd", 0x10), ("data2", 120000), *TURN_ON_TIMELINE]
    # three BUSY waits, each polled at 5 ms until release
    assert sum(1 for e in rig.log if e == ("sleep", POLL)) == 3 * rig.gpio.busy_low_polls
    assert rig.disp.awake and rig.disp.state == "awake"


def test_show_with_rotation_270(frame: np.ndarray) -> None:
    rig = Rig(rotation=270).awake()
    rig.disp.show(frame)
    payload = wire(rig.log)[0][1][0]
    assert payload == pack_frame(frame, 270)
    assert payload != pack_frame(frame, 90)
    assert rig.problems == []


def test_rotation_fn_is_read_on_every_show(rig: Rig, frame: np.ndarray) -> None:
    rig.awake()
    rig.disp.show(frame)
    rig.rotation = 270
    rig.log.clear()
    rig.disp.show(frame)
    assert wire(rig.log)[0][1][0] == pack_frame(frame, 270)


def test_show_records_refresh_duration_and_logs_it(
    rig: Rig, frame: np.ndarray, caplog: pytest.LogCaptureFixture
) -> None:
    rig.awake()
    rig.gpio.low_polls_after[0x12] = 3800  # 19 s of BUSY low after DISPLAY_REFRESH
    with caplog.at_level(logging.INFO, logger="inkframe.display.epd4in0e"):
        rig.disp.show(frame)
    assert rig.disp.last_refresh_seconds == pytest.approx(19.0, abs=1e-6)
    assert any("refresh took 19.0 s" in r.getMessage() for r in caplog.records)


def test_show_accepts_any_integer_dtype(rig: Rig, frame: np.ndarray) -> None:
    rig.awake()
    rig.disp.show(frame.astype(np.int64))
    assert wire(rig.log)[0][1][0] == pack_frame(frame, 90)


def test_frame_followed_by_more_shows_without_reinit(rig: Rig, frame: np.ndarray) -> None:
    rig.awake()
    rig.disp.show(frame)
    rig.disp.show(np.zeros((400, 600), dtype=np.uint8))
    assert [c for c, _ in wire(rig.log)] == [0x10, 0x04, 0x06, 0x12, 0x02] * 2


# --- (3) clear ------------------------------------------------------------------------


def test_clear_floods_white(rig: Rig) -> None:
    rig.awake()
    rig.disp.clear()
    groups = wire(rig.log)
    assert groups[0][0] == 0x10
    assert groups[0][1] == [bytes([0x11]) * 120000]
    assert timeline(rig.log) == [("cmd", 0x10), ("data2", 120000), *TURN_ON_TIMELINE]
    assert rig.disp.awake


# --- (4) sleep ------------------------------------------------------------------------


def test_sleep_sequence(rig: Rig) -> None:
    rig.awake()
    rig.disp.sleep()
    assert rig.log == SLEEP_EVENTS
    rig.assert_powered_down()


def test_sleep_twice_sends_once(rig: Rig) -> None:
    rig.awake()
    rig.disp.sleep()
    rig.disp.sleep()
    assert rig.log == SLEEP_EVENTS


def test_sleep_before_init_is_a_noop(rig: Rig) -> None:
    rig.disp.sleep()
    assert rig.log == []
    assert not rig.disp.awake and rig.disp.state == "asleep"


def test_sleep_after_show(rig: Rig, frame: np.ndarray) -> None:
    rig.awake()
    rig.disp.show(frame)
    rig.log.clear()
    rig.disp.sleep()
    assert rig.log == SLEEP_EVENTS


def test_sleep_never_raises(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.awake()
    rig.spi.fail_writebytes = OSError("bus gone")
    with caplog.at_level(logging.WARNING, logger="inkframe.display.epd4in0e"):
        rig.disp.sleep()
    rig.assert_powered_down()
    assert ("sleep", 2.0) not in rig.log  # no 2 s hold when the deep-sleep command never went out
    assert any("deep-sleep command failed" in r.getMessage() for r in caplog.records)


# --- (5) / (6) misuse touches nothing ---------------------------------------------------


def test_show_before_init_raises_and_touches_nothing(rig: Rig, frame: np.ndarray) -> None:
    with pytest.raises(DisplayNotReady):
        rig.disp.show(frame)
    with pytest.raises(DisplayNotReady):
        rig.disp.clear()
    assert rig.log == []


def test_show_after_sleep_raises_not_ready(rig: Rig, frame: np.ndarray) -> None:
    rig.awake()
    rig.disp.sleep()
    rig.log.clear()
    with pytest.raises(DisplayNotReady):
        rig.disp.show(frame)
    assert rig.log == []


@pytest.mark.parametrize(
    "bad",
    [
        np.zeros((600, 400), dtype=np.uint8),  # portrait
        np.zeros((400, 600, 3), dtype=np.uint8),  # RGB
        np.full((400, 600), 6, dtype=np.uint8),  # index out of range
        np.zeros((400, 600), dtype=np.float32),
    ],
)
def test_bad_frame_raises_value_error_and_touches_nothing(rig: Rig, bad: np.ndarray) -> None:
    rig.awake()
    with pytest.raises(ValueError):
        rig.disp.show(bad)
    assert rig.log == []
    assert rig.disp.awake  # nothing happened to the panel; the caller's sleep() still runs


def test_bad_frame_while_asleep_is_value_error(rig: Rig) -> None:
    with pytest.raises(ValueError):
        rig.disp.show(np.zeros((10, 10), dtype=np.uint8))
    assert rig.log == []


def test_bad_rotation_raises_value_error_and_touches_nothing(frame: np.ndarray) -> None:
    rig = Rig(rotation=0).awake()
    with pytest.raises(ValueError, match="rotation"):
        rig.disp.show(frame)
    assert rig.log == []
    assert rig.disp.awake
    assert rig.problems == []


# --- (7) BUSY timeouts ------------------------------------------------------------------


def test_busy_stuck_during_init_times_out_and_powers_down(rig: Rig) -> None:
    rig.gpio.stuck = True
    with pytest.raises(DisplayTimeout, match="reset for 10s"):
        rig.disp.init()
    rig.assert_powered_down()
    released = rig.log.index(("rst", True), rig.log.index(("rst", False)))
    deep_sleep = rig.log.index(("cmd", 0x07))
    assert 10.0 <= poll_seconds_between(rig.log, released, deep_sleep) <= 10.0 + POLL + 1e-9
    # SPI was open, so deep sleep is attempted before the bus closes and power is cut
    assert rig.log[deep_sleep - 1 :] == SLEEP_EVENTS
    assert ("cmd", 0xAA) not in rig.log
    # sleep() afterwards is a no-op
    n = len(rig.log)
    rig.disp.sleep()
    assert len(rig.log) == n


def test_busy_stuck_after_init_sequence_times_out() -> None:
    rig = Rig()
    rig.gpio.stuck_after = 0x84
    with pytest.raises(DisplayTimeout, match="during init"):
        rig.disp.init()
    rig.assert_powered_down()
    assert rig.problems == []


def test_busy_stuck_during_refresh_times_out_after_busy_timeout(frame: np.ndarray) -> None:
    rig = Rig(busy_timeout_s=60.0).awake()
    rig.gpio.stuck_after = 0x12
    with pytest.raises(DisplayTimeout, match=r"during refresh for 60s"):
        rig.disp.show(frame)
    rig.assert_powered_down()
    start = rig.log.index(("cmd", 0x12))
    deep_sleep = rig.log.index(("cmd", 0x07))
    assert 60.0 <= poll_seconds_between(rig.log, start, deep_sleep) <= 60.0 + POLL + 1e-9
    assert rig.log[deep_sleep - 1 :] == SLEEP_EVENTS
    assert ("cmd", 0x02) not in rig.log
    assert rig.disp.last_refresh_seconds is None
    with pytest.raises(DisplayNotReady):
        rig.disp.show(frame)
    assert rig.problems == []


def test_recovers_with_full_init_after_a_timeout(frame: np.ndarray) -> None:
    rig = Rig().awake()
    rig.gpio.stuck_after = 0x12
    with pytest.raises(DisplayTimeout):
        rig.disp.show(frame)
    rig.gpio.stuck = False
    rig.gpio.stuck_after = None
    rig.log.clear()
    rig.disp.init()
    assert wire(rig.log) == VENDOR_INIT
    assert ("open", (0, 0)) in rig.log
    rig.disp.show(frame)
    rig.disp.sleep()
    rig.assert_powered_down()
    assert rig.problems == []


def test_short_timeout_is_configurable() -> None:
    rig = Rig(short_timeout_s=0.5)
    rig.gpio.stuck = True
    with pytest.raises(DisplayTimeout, match="for 0.5s"):
        rig.disp.init()
    rig.assert_powered_down()
    assert rig.problems == []


# --- (8) I/O failures ----------------------------------------------------------------


def test_spi_error_mid_show_propagates_and_powers_down(rig: Rig, frame: np.ndarray) -> None:
    rig.awake()
    rig.spi.fail_writebytes2 = OSError(5, "Input/output error")
    with pytest.raises(OSError, match="Input/output error"):
        rig.disp.show(frame)
    rig.assert_powered_down()
    assert rig.log[-len(SLEEP_EVENTS) :] == SLEEP_EVENTS


def test_power_down_failures_do_not_mask_the_original_error(rig: Rig, frame: np.ndarray) -> None:
    rig.awake()

    def failing_frame_write(data: bytes) -> None:
        rig.spi.fail_writebytes = OSError("deep sleep write failed")  # the bus is gone for good
        raise OSError("frame write failed")

    rig.spi.writebytes2 = failing_frame_write  # type: ignore[method-assign]
    with pytest.raises(OSError, match="frame write failed"):
        rig.disp.show(frame)
    rig.assert_powered_down()
    assert ("sleep", 2.0) not in rig.log


def test_spi_open_failure_cuts_power_again() -> None:
    rig = Rig()
    rig.spi.fail_open = DisplayError("cannot open /dev/spidev0.0")
    with pytest.raises(DisplayError, match="spidev0.0"):
        rig.disp.init()
    assert rig.log == [
        ("gpio_open", None),
        ("pwr", True),  # power came up, then the bus failed to open
        ("rst", False),
        ("dc", False),
        ("pwr", False),  # no deep sleep without a bus
    ]
    rig.assert_powered_down()
    assert rig.problems == []


def test_gpio_open_failure_touches_nothing_else() -> None:
    rig = Rig()

    def boom() -> None:
        raise DisplayError("cannot claim panel GPIO lines")

    rig.gpio.open = boom  # type: ignore[method-assign]
    with pytest.raises(DisplayError, match="GPIO"):
        rig.disp.init()
    assert rig.log == []
    rig.disp.sleep()
    rig.disp.close()
    assert rig.log == []


def test_sleep_retries_cutting_power_after_a_failed_power_down(rig: Rig, frame: np.ndarray) -> None:
    rig.awake()
    rig.spi.fail_writebytes2 = OSError("boom")
    rig.gpio.fail_pwr_low = 1
    with pytest.raises(OSError, match="boom"):
        rig.disp.show(frame)
    assert rig.gpio.pwr and not rig.spi.is_open and not rig.disp.awake
    rig.log.clear()
    rig.disp.sleep()  # bus already closed: no deep-sleep command, just the lines
    assert rig.log == [("rst", False), ("dc", False), ("pwr", False)]
    rig.assert_powered_down()
    rig.disp.sleep()
    assert len(rig.log) == 3


def test_keyboard_interrupt_during_init_still_powers_down(rig: Rig) -> None:
    def interrupt() -> None:
        rig.gpio.on_busy_read = None
        raise KeyboardInterrupt

    rig.gpio.on_busy_read = interrupt
    with pytest.raises(KeyboardInterrupt):
        rig.disp.init()
    rig.assert_powered_down()
    assert rig.log[-len(SLEEP_EVENTS) :] == SLEEP_EVENTS


def test_keyboard_interrupt_mid_refresh_still_powers_down(rig: Rig, frame: np.ndarray) -> None:
    rig.awake()

    def interrupt() -> None:
        if ("cmd", 0x12) in rig.log:
            rig.gpio.on_busy_read = None
            raise KeyboardInterrupt

    rig.gpio.on_busy_read = interrupt
    with pytest.raises(KeyboardInterrupt):
        rig.disp.show(frame)
    rig.assert_powered_down()


# --- (9) state ----------------------------------------------------------------------------


def test_state_transitions(rig: Rig, frame: np.ndarray) -> None:
    seen: list[str] = []
    rig.gpio.on_busy_read = lambda: seen.append(rig.disp.state)

    assert rig.disp.state == "asleep" and not rig.disp.awake
    rig.disp.init()
    assert set(seen) == {"asleep"}  # not awake until the sequence completed
    assert rig.disp.state == "awake" and rig.disp.awake

    seen.clear()
    rig.disp.show(frame)
    assert set(seen) == {"refreshing"}
    assert rig.disp.awake  # awake throughout the refresh
    assert rig.disp.state == "awake"

    seen.clear()
    rig.disp.clear()
    assert set(seen) == {"refreshing"}
    assert rig.disp.state == "awake"

    rig.disp.sleep()
    assert rig.disp.state == "asleep" and not rig.disp.awake


def test_state_after_failed_refresh_is_asleep(rig: Rig, frame: np.ndarray) -> None:
    rig.awake()
    rig.spi.fail_writebytes2 = OSError("boom")
    with pytest.raises(OSError):
        rig.disp.show(frame)
    assert rig.disp.state == "asleep"


# --- threading -----------------------------------------------------------------------------


def test_concurrent_sleep_waits_for_running_refresh(rig: Rig, frame: np.ndarray) -> None:
    rig.awake()
    sleeper: list[threading.Thread] = []

    def start_sleeper_during_refresh() -> None:
        if not sleeper and ("cmd", 0x12) in rig.log:
            t = threading.Thread(target=rig.disp.sleep)
            sleeper.append(t)
            t.start()
            t.join(timeout=0.05)
            assert t.is_alive(), "sleep() ran while a refresh held the panel"

    rig.gpio.on_busy_read = start_sleeper_during_refresh
    rig.disp.show(frame)
    sleeper[0].join(timeout=5)
    assert not sleeper[0].is_alive()
    kinds = [e for e in rig.log if e[0] == "cmd"]
    assert kinds[-2:] == [("cmd", 0x02), ("cmd", 0x07)]  # deep sleep only after power-off
    rig.assert_powered_down()


# --- close / protocol / construction ------------------------------------------------------


def test_close_sleeps_and_releases_gpio(rig: Rig) -> None:
    rig.awake()
    rig.disp.close()
    assert rig.log == SLEEP_EVENTS + [("gpio_close", None)]
    rig.disp.close()
    assert rig.log == SLEEP_EVENTS + [("gpio_close", None)]
    # a later init claims the lines again
    rig.disp.init()
    assert rig.gpio.opens == 2 and rig.disp.awake


def test_close_before_init_is_a_noop(rig: Rig) -> None:
    rig.disp.close()
    assert rig.log == []


def test_implements_display_protocol(rig: Rig) -> None:
    assert isinstance(rig.disp, Display)
    assert (rig.disp.kind, rig.disp.width, rig.disp.height) == ("epd", 600, 400)


def test_factory_signature_constructs_without_touching_hardware() -> None:
    disp = epd.EPD4in0e(rotation_fn=lambda: 90, busy_timeout_s=60.0)
    assert not disp.awake and disp.state == "asleep" and disp.last_refresh_seconds is None
    disp.sleep()  # still nothing opened


def test_default_rotation_is_90(frame: np.ndarray) -> None:
    log: list[Event] = []
    problems: list[str] = []
    clock = FakeClock(log)
    gpio = FakeGpio(log, problems)
    spi = FakeSpi(log, problems, gpio)
    disp = epd.EPD4in0e(spi=spi, gpio=gpio, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
    disp.init()
    log.clear()
    disp.show(frame)
    assert wire(log)[0][1][0] == pack_frame(frame, 90)
    assert problems == []


@pytest.mark.parametrize("kwargs", [{"busy_timeout_s": 0}, {"short_timeout_s": -1.0}])
def test_rejects_non_positive_timeouts(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="timeouts must be positive"):
        epd.EPD4in0e(**kwargs)  # type: ignore[arg-type]


def test_module_constants_match_vendor() -> None:
    assert [(c, list(d)) for c, d in epd.INIT_SEQUENCE] == VENDOR_INIT
    assert list(epd.TURN_ON_06_DATA) == [0x6F, 0x1F, 0x17, 0x27]
    assert epd.CLEAR_FRAME == bytes([0x11]) * 120000
    assert (epd.RST_PIN, epd.DC_PIN, epd.BUSY_PIN, epd.PWR_PIN) == (17, 25, 24, 18)


# --- (10) real backends on a machine without the hardware libraries ------------------------


def _hw_libs_present() -> bool:
    return any(importlib.util.find_spec(name) is not None for name in ("spidev", "gpiozero"))


#: on the Pi the real backends would claim the lines and drive PWR: never there
needs_no_hw_libs = pytest.mark.skipif(
    _hw_libs_present(), reason="spidev/gpiozero importable here: would drive real pins"
)


def test_import_has_no_hardware_side_effects() -> None:
    code = (
        "import sys, inkframe.display.epd4in0e as m\n"
        "bad = [n for n in ('spidev', 'gpiozero', 'lgpio', 'RPi', 'PIL') if n in sys.modules]\n"
        "assert not bad, bad\n"
        "m.EPD4in0e(rotation_fn=lambda: 90, busy_timeout_s=60.0)\n"
        "bad = [n for n in ('spidev', 'gpiozero', 'lgpio') if n in sys.modules]\n"
        "assert not bad, bad\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


@needs_no_hw_libs
def test_real_backends_raise_display_error_without_hw_libs() -> None:
    disp = epd.EPD4in0e(rotation_fn=lambda: 90, busy_timeout_s=60.0)
    with pytest.raises(DisplayError) as excinfo:
        disp.init()
    message = str(excinfo.value)
    assert "gpiozero" in message and "spidev" in message
    assert isinstance(excinfo.value.__cause__, ImportError)
    assert not disp.awake and disp.state == "asleep"
    disp.sleep()
    disp.close()


@needs_no_hw_libs
def test_each_real_backend_wraps_its_import_error() -> None:
    with pytest.raises(DisplayError, match="cannot import spidev") as spi_err:
        epd.SpidevBus().open(0, 0)
    assert isinstance(spi_err.value.__cause__, ImportError)
    with pytest.raises(DisplayError, match="cannot import gpiozero") as gpio_err:
        epd.GpiozeroGpio().open()
    assert isinstance(gpio_err.value.__cause__, ImportError)
    # using either before open is a clear error, not an AttributeError
    with pytest.raises(DisplayError, match="not open"):
        epd.SpidevBus().writebytes([0])
    with pytest.raises(DisplayError, match="not open"):
        epd.GpiozeroGpio().read_busy()
    epd.SpidevBus().close()
    epd.GpiozeroGpio().close()
