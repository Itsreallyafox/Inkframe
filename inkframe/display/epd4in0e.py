"""Driver for the Waveshare 4inch e-Paper HAT+ (E) (E Ink Spectra 6, 600 x 400).

A trimmed port of the vendor driver (``vendor/waveshare/epd4in0e.py`` +
``epdconfig.py``, cross-checked against ``EPD_4in0e.c``; see
``vendor/waveshare/README.md`` for provenance and licence) behind the
``Display`` protocol.  The bytes on the wire are the vendor's, in the vendor's
order; what changed is everything around them:

* hardware access goes through two tiny injectable backends (``SpiBus`` and
  ``Gpio``) so the exact wire sequence is unit-tested on the desktop with
  fakes; the real backends import ``spidev`` / ``gpiozero`` lazily, so this
  module imports cleanly anywhere and has no import-time side effects (the
  vendor ``epdconfig`` claims GPIO lines and shells out at import);
* every BUSY wait has a timeout (the vendor loops forever) and raises
  ``DisplayTimeout``;
* any failure inside ``init()``/``show()``/``clear()`` runs the power-down path
  (deep sleep if the bus is up, close the bus, RST/DC/PWR low) before
  re-raising, so no error path leaves the panel powered;
* the 200 ms settles that the C driver adds after the power-on BUSY wait and
  the ESP32 driver adds after the power-off BUSY wait are both included;
* no PIL, no ``getbuffer``: frames are packed by
  ``inkframe.display.palette.pack_frame``, which reproduces the vendor byte
  layout exactly.

Wire protocol (vendor ``epd4in0e.py``):

* command byte: DC low, one ``writebytes([b])``; data byte: DC high, one
  ``writebytes([b])``.  One byte per SPI transfer, like the vendor.
* frame: command ``0x10``, then DC high and a single ``writebytes2`` of the
  120000 packed bytes (py-spidev chunks it at ``bufsiz`` internally;
  ``writebytes`` refuses more than 4096 bytes).
* CS is the hardware CE0 line driven by spidev; this driver never touches it
  (the vendor Python's software-CS writes are no-ops too).
* BUSY (BCM 24, pull-down) reads LOW while the controller is busy and HIGH
  when idle.  The comment above the vendor C ``ReadBusyH`` says the opposite;
  the code (and this driver) treats LOW as busy.

Lifecycle: ``init()`` -> ``show()``/``clear()`` (any number) -> ``sleep()``.
After ``sleep()`` the controller only wakes through a full ``init()``; nothing
here caches an "initialised" flag across sleep.  ``close()`` additionally
releases the GPIO lines and is meant for process shutdown.

Thread-safety: ``init``/``show``/``clear``/``sleep``/``close`` serialise on one
re-entrant lock, so a ``sleep()`` from another thread waits for a running
refresh to finish.  ``awake``, ``state`` and ``last_refresh_seconds`` are plain
attribute reads and never block, so a status endpoint can poll them during the
~19 s refresh.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any, Protocol

import numpy as np

from inkframe.display.base import DisplayError, DisplayNotReady, DisplayTimeout, validate_frame
from inkframe.display.palette import (
    FRAME_BYTES,
    PANEL_CODES,
    PANEL_HEIGHT,
    PANEL_WIDTH,
    WHITE,
    pack_frame,
)

log = logging.getLogger(__name__)

# --- pins and bus (BCM numbering; vendor epdconfig.py 43-47 and 142-145) -----
RST_PIN = 17
DC_PIN = 25
BUSY_PIN = 24
PWR_PIN = 18  # switches the HAT's 5 V rail
SPI_BUS = 0
SPI_DEVICE = 0  # /dev/spidev0.0, chip select = hardware CE0 (BCM 8)
SPI_MAX_SPEED_HZ = 4_000_000
SPI_MODE = 0

# --- timing, seconds ----------------------------------------------------------
RESET_HIGH_S = 0.020
RESET_LOW_S = 0.002
POST_RESET_S = 0.030  # after the first BUSY wait in init
BUSY_POLL_S = 0.005
BUSY_SETTLE_S = 0.200  # after every BUSY release (vendor ReadBusyH)
POWER_ON_SETTLE_S = 0.200  # after the power-on BUSY wait (vendor C driver)
TURN_ON_06_SETTLE_S = 0.200  # after the second 0x06 in TurnOnDisplay
POWER_OFF_SETTLE_S = 0.200  # after the power-off BUSY wait (vendor ESP32 driver)
DEEP_SLEEP_HOLD_S = 2.0  # deep sleep -> 5 V off; vendor C test: "important, at least 2s"

# --- commands -----------------------------------------------------------------
# The vendor names only POWER_ON, DISPLAY_REFRESH, POWER_OFF and DEEP_SLEEP
# (plus "CMDH" for 0xAA).  0x10 is named here for what the vendor uses it for;
# every other register is undocumented and copied byte for byte.
CMD_POWER_OFF = 0x02
CMD_POWER_ON = 0x04
CMD_DEEP_SLEEP = 0x07
CMD_DATA_START = 0x10  # frame data follows
CMD_DISPLAY_REFRESH = 0x12
DEEP_SLEEP_CHECK_CODE = 0xA5

#: vendor epd4in0e.py 122-181 (identical in EPD_4in0e.c 126-185)
INIT_SEQUENCE: tuple[tuple[int, bytes], ...] = (
    (0xAA, bytes((0x49, 0x55, 0x20, 0x08, 0x09, 0x18))),  # "CMDH" in the vendor C
    (0x01, bytes((0x3F,))),
    (0x00, bytes((0x5F, 0x69))),
    (0x05, bytes((0x40, 0x1F, 0x1F, 0x2C))),
    (0x08, bytes((0x6F, 0x1F, 0x1F, 0x22))),
    (0x06, bytes((0x6F, 0x1F, 0x17, 0x17))),  # last byte differs from TURN_ON_06_DATA
    (0x03, bytes((0x00, 0x54, 0x00, 0x44))),
    (0x60, bytes((0x02, 0x00))),
    (0x30, bytes((0x08,))),
    (0x50, bytes((0x3F,))),
    (0x61, bytes((0x01, 0x90, 0x02, 0x58))),  # 0x0190 x 0x0258 = 400 x 600, native portrait
    (0xE3, bytes((0x2F,))),
    (0x84, bytes((0x01,))),
)

#: data of the 0x06 sent again in TurnOnDisplay (vendor epd4in0e.py 99-103, "Second
#: setting" in the C); differs from the init 0x06 in its last byte, both are intentional
TURN_ON_06_DATA: bytes = bytes((0x6F, 0x1F, 0x17, 0x27))

#: white in both nibbles (panel code 1 -> 0x11), what the vendor ``Clear()`` floods with
CLEAR_BYTE = (int(PANEL_CODES[WHITE]) << 4) | int(PANEL_CODES[WHITE])
CLEAR_FRAME: bytes = bytes((CLEAR_BYTE,)) * FRAME_BYTES

_HW_HINT = (
    "the epd display needs the spidev and gpiozero Python modules (on Raspberry Pi OS: "
    "apt install python3-spidev python3-gpiozero python3-lgpio, with the venv created "
    "using --system-site-packages); use INKFRAME_DISPLAY=mock elsewhere"
)


# --- hardware backends ----------------------------------------------------------


class SpiBus(Protocol):
    """The slice of ``spidev.SpiDev`` the driver uses."""

    def open(self, bus: int, device: int) -> None:
        """Open ``/dev/spidev<bus>.<device>``."""

    def configure(self, max_speed_hz: int, mode: int) -> None:
        """Set clock speed and SPI mode on the open bus."""

    def writebytes(self, data: list[int]) -> None:
        """One short transfer (the driver always sends a single byte)."""

    def writebytes2(self, data: bytes) -> None:
        """One arbitrarily long transfer (the 120000-byte frame)."""

    def close(self) -> None:
        """Close the bus."""


class Gpio(Protocol):
    """The four panel control lines.

    ``open()`` claims the lines (outputs start LOW) and is idempotent; the
    lines stay claimed until ``close()``.  ``read_busy()`` returns True when
    the BUSY line is HIGH, which means the panel is *idle*.
    """

    def open(self) -> None:
        """Claim RST/DC/PWR as outputs (initially low) and BUSY as a pulled-down input."""

    def set_rst(self, high: bool) -> None:
        """Drive the reset line."""

    def set_dc(self, high: bool) -> None:
        """Drive the data/command line (low = command, high = data)."""

    def set_pwr(self, high: bool) -> None:
        """Switch the HAT's 5 V rail."""

    def read_busy(self) -> bool:
        """True if BUSY reads HIGH (panel idle)."""

    def close(self) -> None:
        """Release all lines."""


class SpidevBus:
    """``SpiBus`` on py-spidev.  ``spidev`` is imported on ``open()``, not at import."""

    def __init__(self) -> None:
        self._dev: Any = None

    def open(self, bus: int, device: int) -> None:
        if self._dev is not None:
            raise DisplayError("SPI bus is already open")
        try:
            import spidev  # type: ignore[import-not-found]
        except ImportError as exc:
            raise DisplayError(f"cannot import spidev ({exc}); {_HW_HINT}") from exc
        dev = spidev.SpiDev()
        try:
            dev.open(bus, device)
        except OSError as exc:
            raise DisplayError(
                f"cannot open /dev/spidev{bus}.{device}: {exc} (is SPI enabled with "
                "'raspi-config nonint do_spi 0' and is the user in group 'spi'?)"
            ) from exc
        self._dev = dev

    def configure(self, max_speed_hz: int, mode: int) -> None:
        dev = self._require()
        dev.max_speed_hz = max_speed_hz
        dev.mode = mode

    def writebytes(self, data: list[int]) -> None:
        self._require().writebytes(data)

    def writebytes2(self, data: bytes) -> None:
        self._require().writebytes2(data)

    def close(self) -> None:
        dev, self._dev = self._dev, None
        if dev is not None:
            dev.close()

    def _require(self) -> Any:
        if self._dev is None:
            raise DisplayError("SPI bus is not open")
        return self._dev


class GpiozeroGpio:
    """``Gpio`` on gpiozero (lgpio pin factory on Pi OS Trixie), wired like the vendor.

    RST/DC/PWR are ``gpiozero.LED`` (created low), BUSY is
    ``gpiozero.Button(pin, pull_up=False)``.  The devices are created on the
    first ``open()`` and reused until ``close()``: lgpio line claims are
    exclusive, so creating them twice in one process fails.
    """

    def __init__(
        self,
        rst_pin: int = RST_PIN,
        dc_pin: int = DC_PIN,
        pwr_pin: int = PWR_PIN,
        busy_pin: int = BUSY_PIN,
    ) -> None:
        self._pins = (rst_pin, dc_pin, pwr_pin, busy_pin)
        self._rst: Any = None
        self._dc: Any = None
        self._pwr: Any = None
        self._busy: Any = None

    def open(self) -> None:
        if self._busy is not None:
            return
        try:
            import gpiozero  # type: ignore[import-not-found]
        except ImportError as exc:
            raise DisplayError(f"cannot import gpiozero ({exc}); {_HW_HINT}") from exc
        rst_pin, dc_pin, pwr_pin, busy_pin = self._pins
        created: list[Any] = []
        try:
            for pin in (rst_pin, dc_pin, pwr_pin):
                created.append(gpiozero.LED(pin))
            created.append(gpiozero.Button(busy_pin, pull_up=False))
        except Exception as exc:
            for dev in reversed(created):
                try:
                    dev.close()
                except Exception:
                    log.warning(
                        "could not release %r after a failed GPIO claim", dev, exc_info=True
                    )
            raise DisplayError(
                f"cannot claim panel GPIO lines (BCM {rst_pin}/{dc_pin}/{pwr_pin}/{busy_pin}): "
                f"{exc} (is the user in group 'gpio', and is no other process using the panel?)"
            ) from exc
        self._rst, self._dc, self._pwr, self._busy = created

    def set_rst(self, high: bool) -> None:
        self._write(self._rst, high)

    def set_dc(self, high: bool) -> None:
        self._write(self._dc, high)

    def set_pwr(self, high: bool) -> None:
        self._write(self._pwr, high)

    def read_busy(self) -> bool:
        if self._busy is None:
            raise DisplayError("GPIO lines are not open")
        return self._busy.value == 1

    def close(self) -> None:
        outputs = [d for d in (self._rst, self._dc, self._pwr) if d is not None]
        devices = outputs + ([self._busy] if self._busy is not None else [])
        self._rst = self._dc = self._pwr = self._busy = None
        first_error: Exception | None = None
        for dev in outputs:  # RST, DC, then PWR low before letting go of the lines
            try:
                dev.off()
            except Exception as exc:
                first_error = first_error or exc
        for dev in devices:
            try:
                dev.close()
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise DisplayError(
                f"error while releasing panel GPIO lines: {first_error}"
            ) from first_error

    @staticmethod
    def _write(dev: Any, high: bool) -> None:
        if dev is None:
            raise DisplayError("GPIO lines are not open")
        if high:
            dev.on()
        else:
            dev.off()


# --- the driver -------------------------------------------------------------------


def _default_rotation() -> int:
    return 90


class EPD4in0e:
    """The real panel, implementing ``inkframe.display.base.Display``.

    ``rotation_fn`` is called on every ``show()`` and returns 90 (the vendor
    ``getbuffer`` orientation) or 270 (panel mounted the other way up).
    ``busy_timeout_s`` bounds the ~19 s refresh wait; ``short_timeout_s``
    bounds every other BUSY wait (reset, init, power on/off).  ``spi``,
    ``gpio``, ``sleep_fn`` and ``monotonic_fn`` are injectable for tests; by
    default the real spidev/gpiozero backends and the ``time`` module are
    used.
    """

    kind = "epd"
    width = PANEL_WIDTH
    height = PANEL_HEIGHT

    def __init__(
        self,
        rotation_fn: Callable[[], int] | None = None,
        busy_timeout_s: float = 60.0,
        *,
        spi: SpiBus | None = None,
        gpio: Gpio | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        short_timeout_s: float = 10.0,
    ) -> None:
        if busy_timeout_s <= 0 or short_timeout_s <= 0:
            raise ValueError(
                f"timeouts must be positive, got busy_timeout_s={busy_timeout_s!r}, "
                f"short_timeout_s={short_timeout_s!r}"
            )
        self._rotation_fn = rotation_fn if rotation_fn is not None else _default_rotation
        self._busy_timeout_s = float(busy_timeout_s)
        self._short_timeout_s = float(short_timeout_s)
        self._spi: SpiBus = spi if spi is not None else SpidevBus()
        self._gpio: Gpio = gpio if gpio is not None else GpiozeroGpio()
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._lock = threading.RLock()
        self._gpio_open = False
        self._spi_open = False
        self._pwr_on = False  # True from the moment PWR is asked to go high until it is driven low
        self._awake = False
        self._state = "asleep"
        #: seconds the panel held BUSY low for the last completed refresh (the 0x12 wait)
        self.last_refresh_seconds: float | None = None

    # --- status (lock-free reads) ---------------------------------------------
    @property
    def awake(self) -> bool:
        """True between a successful ``init()`` and the next ``sleep()`` or failure."""
        return self._awake

    @property
    def state(self) -> str:
        """``"asleep"``, ``"awake"`` or ``"refreshing"`` (inside ``show``/``clear``)."""
        return self._state

    # --- Display protocol -------------------------------------------------------
    def init(self) -> None:
        """Power the panel, open the bus and run the vendor init sequence.

        Safe to call when already awake (the sequence is re-run).  On any
        failure the panel is powered down before the exception propagates.
        """
        with self._lock:
            self._awake = False
            self._state = "asleep"
            try:
                self._open_hardware()
                self._reset()
                self._wait_busy(self._short_timeout_s, "reset")
                self._sleep(POST_RESET_S)
                for cmd, data in INIT_SEQUENCE:
                    self._command(cmd, data)
                self._wait_busy(self._short_timeout_s, "init")
            except BaseException as exc:
                self._power_down_after_failure("init", exc)
                raise
            self._awake = True
            self._state = "awake"
            log.debug("panel initialised")

    def show(self, frame: np.ndarray) -> None:
        """Send a ``(400, 600)`` palette-index frame and run a full refresh.

        Blocks for the whole refresh (~19 s).  Raises ``ValueError`` for a bad
        frame or rotation and ``DisplayNotReady`` if not awake, both before any
        hardware access.  Any failure after that powers the panel down,
        leaves it asleep and re-raises.
        """
        with self._lock:
            checked = validate_frame(frame)
            self._require_awake("show")
            buf = pack_frame(checked, self._rotation_fn())
            self._refresh(buf, "show")

    def clear(self) -> None:
        """Flood the panel white with a full refresh (vendor ``Clear()``)."""
        with self._lock:
            self._require_awake("clear")
            self._refresh(CLEAR_FRAME, "clear")

    def sleep(self) -> None:
        """Deep-sleep the controller and cut panel power.

        Idempotent and never raises (problems are logged at WARNING): a no-op
        before ``init()``, after a failure (which already powered down) and on
        a second call.
        """
        with self._lock:
            if self._spi_open or self._pwr_on:
                self._power_down()
                log.debug("panel in deep sleep, 5 V off")
            self._awake = False
            self._state = "asleep"

    def close(self) -> None:
        """``sleep()`` and release the GPIO lines; for process shutdown.  Never raises.

        A later ``init()`` claims the lines again.
        """
        with self._lock:
            self.sleep()
            if self._gpio_open:
                self._gpio_open = False
                try:
                    self._gpio.close()
                except Exception:
                    log.warning("error releasing panel GPIO lines", exc_info=True)

    # --- internals ----------------------------------------------------------------
    def _require_awake(self, what: str) -> None:
        if not self._awake:
            raise DisplayNotReady(f"{what}() called while the panel is asleep; call init() first")

    def _open_hardware(self) -> None:
        """Claim GPIO (once per process), 5 V on, then open and configure SPI."""
        if not self._gpio_open:
            self._gpio.open()
            self._gpio_open = True
        self._pwr_on = True
        self._gpio.set_pwr(True)
        if not self._spi_open:
            self._spi.open(SPI_BUS, SPI_DEVICE)
            self._spi_open = True
            self._spi.configure(SPI_MAX_SPEED_HZ, SPI_MODE)

    def _reset(self) -> None:
        self._gpio.set_rst(True)
        self._sleep(RESET_HIGH_S)
        self._gpio.set_rst(False)
        self._sleep(RESET_LOW_S)
        self._gpio.set_rst(True)
        self._sleep(RESET_HIGH_S)

    def _send_command(self, cmd: int) -> None:
        self._gpio.set_dc(False)
        self._spi.writebytes([cmd])

    def _send_data(self, value: int) -> None:
        self._gpio.set_dc(True)
        self._spi.writebytes([value])

    def _command(self, cmd: int, data: Iterable[int] = ()) -> None:
        self._send_command(cmd)
        for value in data:
            self._send_data(value)

    def _wait_busy(self, timeout_s: float, what: str) -> float:
        """Poll until BUSY reads HIGH, settle 200 ms, return seconds spent busy."""
        start = self._monotonic()
        deadline = start + timeout_s
        while not self._gpio.read_busy():
            if self._monotonic() >= deadline:
                raise DisplayTimeout(f"panel BUSY stuck low during {what} for {timeout_s:g}s")
            self._sleep(BUSY_POLL_S)
        busy_s = self._monotonic() - start
        log.debug("BUSY released after %.3f s (%s)", busy_s, what)
        self._sleep(BUSY_SETTLE_S)
        return busy_s

    def _turn_on_display(self) -> float:
        """Vendor ``TurnOnDisplay``; returns the seconds the refresh kept BUSY low."""
        self._command(CMD_POWER_ON)
        self._wait_busy(self._short_timeout_s, "power on")
        self._sleep(POWER_ON_SETTLE_S)
        self._command(0x06, TURN_ON_06_DATA)
        self._sleep(TURN_ON_06_SETTLE_S)
        self._command(CMD_DISPLAY_REFRESH, (0x00,))
        refresh_s = self._wait_busy(self._busy_timeout_s, "refresh")
        self._command(CMD_POWER_OFF, (0x00,))
        self._wait_busy(self._short_timeout_s, "power off")
        self._sleep(POWER_OFF_SETTLE_S)
        return refresh_s

    def _refresh(self, buf: bytes, what: str) -> None:
        if len(buf) != FRAME_BYTES:
            raise DisplayError(f"packed frame is {len(buf)} bytes, expected {FRAME_BYTES}")
        self._state = "refreshing"
        start = self._monotonic()
        try:
            self._send_command(CMD_DATA_START)
            self._gpio.set_dc(True)
            self._spi.writebytes2(buf)
            refresh_s = self._turn_on_display()
        except BaseException as exc:
            self._power_down_after_failure(what, exc)
            raise
        self.last_refresh_seconds = refresh_s
        self._state = "awake"
        log.info(
            "refresh took %.1f s (%s, %.1f s including transfer and power cycling)",
            refresh_s,
            what,
            self._monotonic() - start,
        )

    def _power_down_after_failure(self, what: str, exc: BaseException) -> None:
        log.warning("%s failed (%s: %s); powering the panel down", what, type(exc).__name__, exc)
        self._power_down()

    def _power_down(self) -> None:
        """Deep sleep if the bus is open, close the bus, drive RST, DC, PWR low.

        Best effort: each step's ``Exception`` is logged and the remaining
        steps still run; the line-lowering runs even if the 2 s hold is
        interrupted.
        """
        self._awake = False
        self._state = "asleep"
        try:
            if self._spi_open:
                try:
                    self._command(CMD_DEEP_SLEEP, (DEEP_SLEEP_CHECK_CODE,))
                    self._sleep(DEEP_SLEEP_HOLD_S)
                except Exception:
                    log.warning(
                        "deep-sleep command failed; cutting panel power anyway", exc_info=True
                    )
        finally:
            if self._spi_open:
                self._spi_open = False
                try:
                    self._spi.close()
                except Exception:
                    log.warning("error closing the SPI bus", exc_info=True)
            if self._gpio_open:
                _drive_low("RST", self._gpio.set_rst)
                _drive_low("DC", self._gpio.set_dc)
                if _drive_low("PWR", self._gpio.set_pwr):
                    self._pwr_on = False


def _drive_low(name: str, setter: Callable[[bool], None]) -> bool:
    """``setter(False)``; log instead of raising.  True on success."""
    try:
        setter(False)
    except Exception:
        log.warning("could not drive %s low", name, exc_info=True)
        return False
    return True
