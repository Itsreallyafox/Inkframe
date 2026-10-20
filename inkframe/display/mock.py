"""Desktop / test stand-in for the panel.

``MockDisplay`` honours the same contract as the real driver (see
``inkframe.display.base``), including "``show()`` while asleep is an error",
so scheduler bugs surface on the desktop.  Instead of driving SPI it writes
what the panel would show:

* ``<data>/last_frame.png``: the frame in the panel's (measured) colours, RGB
  600 x 400, i.e. what the owner would see on the wall;
* ``<data>/last_frame.bin``: the exact 120000 bytes the driver would send.

Both files are replaced atomically so a reader never sees half a frame.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from inkframe.display.base import DisplayNotReady, validate_frame
from inkframe.display.palette import (
    MEASURED_DEFAULT,
    PANEL_HEIGHT,
    PANEL_WIDTH,
    WHITE,
    Palette,
    indices_to_image,
    pack_frame,
    unpack_frame,
)
from inkframe.paths import DataPaths, atomic_write_bytes

log = logging.getLogger("inkframe.display.mock")


class MockDisplay:
    """A ``Display`` that renders to files instead of a panel."""

    kind = "mock"
    width = PANEL_WIDTH
    height = PANEL_HEIGHT

    def __init__(
        self,
        data_dir: Path,
        palette_fn: Callable[[], Palette] | None = None,
        refresh_seconds: float = 0.0,
        rotation_fn: Callable[[], int] | None = None,
    ) -> None:
        """``palette_fn`` gives the colours for ``last_frame.png`` (default: the
        measured starting palette); ``rotation_fn`` the packing rotation for
        ``last_frame.bin`` (default 90); ``refresh_seconds`` makes ``show()`` and
        ``clear()`` block like the real ~19 s refresh."""
        self._paths = DataPaths(Path(data_dir))
        self._palette_fn = palette_fn
        self._rotation_fn = rotation_fn
        self._refresh_seconds = float(refresh_seconds)
        # init/show/clear/sleep serialise like the real driver: a sleep() from
        # another thread waits for a running refresh to finish
        self._lock = threading.RLock()
        self._awake = False
        self._state = "asleep"
        #: UTC time the last ``show()``/``clear()`` finished
        self.last_show_at: datetime | None = None
        self.show_count = 0
        self.clear_count = 0
        #: copy of the last frame shown (all WHITE after ``clear()``)
        self.last_frame: np.ndarray | None = None

    @property
    def awake(self) -> bool:
        return self._awake

    @property
    def state(self) -> str:
        """``"asleep"``, ``"awake"`` or ``"refreshing"``, like the real driver."""
        return self._state

    @property
    def png_path(self) -> Path:
        return self._paths.last_frame_png

    @property
    def bin_path(self) -> Path:
        return self._paths.last_frame_bin

    def init(self) -> None:
        with self._lock:
            self._awake = True
            self._state = "awake"
        log.info("mock init: awake")

    def show(self, frame: np.ndarray) -> None:
        frame = validate_frame(frame)
        self._refresh(frame, "show")

    def clear(self) -> None:
        self._refresh(np.full((PANEL_HEIGHT, PANEL_WIDTH), WHITE, dtype=np.uint8), "clear")

    def sleep(self) -> None:
        with self._lock:
            self._awake = False
            self._state = "asleep"
        log.info("mock sleep: asleep")

    def close(self) -> None:
        """Nothing to release; same as ``sleep()``."""
        self.sleep()

    def _refresh(self, frame: np.ndarray, what: str) -> None:
        with self._lock:
            if not self._awake:
                raise DisplayNotReady(
                    f"{what}() called while the display is asleep; call init() first"
                )
            started = time.monotonic()
            self._state = "refreshing"
            try:
                palette = self._palette_fn() if self._palette_fn else MEASURED_DEFAULT
                rotation = self._rotation_fn() if self._rotation_fn else 90
                packed = pack_frame(frame, rotation)
                png = io.BytesIO()
                indices_to_image(frame, palette).save(png, format="PNG", compress_level=6)
                if self._refresh_seconds > 0:
                    time.sleep(self._refresh_seconds)
                self._paths.root.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(self.png_path, png.getvalue())
                atomic_write_bytes(self.bin_path, packed)
            finally:
                self._state = "awake"
            self.last_frame = frame.copy()
            self.last_show_at = datetime.now(timezone.utc)
            if what == "show":
                self.show_count += 1
            else:
                self.clear_count += 1
        log.info(
            "mock %s: wrote %s and %s (palette %s, rotation %d, %.0f ms)",
            what,
            self.png_path.name,
            self.bin_path.name,
            palette.name,
            rotation,
            (time.monotonic() - started) * 1000,
        )


def read_last_frame(data_dir: Path, rotation: int = 90) -> np.ndarray:
    """Decode ``<data_dir>/last_frame.bin`` back to a ``(400, 600)`` index frame,
    for tests and tools that want to check what was "sent to the panel"."""
    return unpack_frame(DataPaths(Path(data_dir)).last_frame_bin.read_bytes(), rotation)
