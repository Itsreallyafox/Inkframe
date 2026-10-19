"""Display protocol: the one interface the scheduler talks to.

Two implementations exist: ``epd4in0e.EPD4in0e`` (the real panel) and
``mock.MockDisplay`` (desktop + tests).  Both take *logical frames*:
``numpy.uint8`` arrays of shape ``(400, 600)`` (rows, columns: landscape)
whose values are palette indices 0..5 as defined in
``inkframe.display.palette``.  Packing to the controller's nibble format and
the portrait rotation are the driver's business, never the caller's.

Panel-care contract (see INKFRAME_PLAN.md section 2) every implementation
must honour:

* ``init()`` powers the panel up and runs the controller init sequence.
* ``show(frame)`` blocks for the whole refresh (about 19 s on hardware).
* ``sleep()`` puts the controller in deep sleep and cuts panel power.  It must
  be safe to call at any time, including after a failed ``init()``/``show()``
  and twice in a row.  Callers wrap every refresh in ``try/finally: sleep()``.
* Image data sent while asleep is ignored by the hardware, so ``show()``
  before ``init()`` is a programming error and raises ``DisplayNotReady``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from inkframe.display.palette import PANEL_HEIGHT, PANEL_WIDTH


class DisplayError(RuntimeError):
    """Base class for display failures."""


class DisplayTimeout(DisplayError):
    """The panel's BUSY line did not release within the allowed time."""


class DisplayNotReady(DisplayError):
    """``show()`` was called without a preceding successful ``init()``."""


@runtime_checkable
class Display(Protocol):
    """Everything the scheduler needs from a panel."""

    #: ``"epd"`` or ``"mock"``; reported by ``/api/status``.
    kind: str
    #: logical (landscape) size the rest of the app renders to: 600 x 400
    width: int
    height: int

    def init(self) -> None:
        """Power the panel on and run the controller init sequence."""

    def show(self, frame: np.ndarray) -> None:
        """Send a (400, 600) uint8 palette-index frame and run a full refresh.

        Blocks until the refresh finishes.  Raises ``DisplayNotReady`` if the
        display is asleep, ``ValueError`` on a wrong shape/dtype/value range,
        ``DisplayTimeout`` if the panel hangs.
        """

    def clear(self) -> None:
        """Flood the panel white with a full refresh (``clear_before_refresh``)."""

    def sleep(self) -> None:
        """Deep-sleep the controller and cut panel power.  Idempotent, never raises."""

    def close(self) -> None:
        """``sleep()`` and release any OS resources (GPIO lines); for process
        shutdown.  Idempotent, never raises.  A later ``init()`` re-acquires them."""

    @property
    def awake(self) -> bool:
        """True between a successful ``init()`` and the next ``sleep()``."""

    @property
    def state(self) -> str:
        """``"asleep"`` | ``"awake"`` | ``"refreshing"`` (inside ``show``/``clear``).

        Lock-free so ``/api/status`` can poll it during a 19 s refresh.
        """


def validate_frame(frame: np.ndarray) -> np.ndarray:
    """Check a logical frame and return it as a contiguous uint8 array.

    Raises ``ValueError`` with a precise message on any violation.
    """
    if not isinstance(frame, np.ndarray):
        raise ValueError(f"frame must be a numpy array, got {type(frame).__name__}")
    if frame.shape != (PANEL_HEIGHT, PANEL_WIDTH):
        raise ValueError(
            f"frame must have shape ({PANEL_HEIGHT}, {PANEL_WIDTH}) (rows, cols), got {frame.shape}"
        )
    if frame.dtype != np.uint8:
        if not np.issubdtype(frame.dtype, np.integer):
            raise ValueError(f"frame dtype must be an integer type, got {frame.dtype}")
        if frame.size and (frame.min() < 0 or frame.max() > 255):
            raise ValueError("frame values out of uint8 range")
        frame = frame.astype(np.uint8)
    if frame.size and frame.max() > 5:
        raise ValueError(f"frame contains palette index {int(frame.max())}; valid indices are 0..5")
    return np.ascontiguousarray(frame)
