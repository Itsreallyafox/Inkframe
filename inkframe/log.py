"""Logging setup: one human-readable stream handler plus an in-memory ring buffer.

journald adds its own timestamps on the Pi; we still include ``%(asctime)s``
so desktop runs are readable.  The ring buffer backs ``/api/system/logs``.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

LOG_FORMAT = "%(asctime)s %(levelname).1s %(name)s: %(message)s"
RING_BUFFER_LINES = 500

#: marks handlers installed by ``configure_logging`` so re-configuring replaces
#: only ours (pytest's capture handlers on the root logger stay untouched)
_OWNED_ATTR = "_inkframe_owned"


class RingBufferHandler(logging.Handler):
    """Keeps the last ``maxlen`` formatted log *lines* in memory.

    A record with a traceback becomes several lines, so the buffer holds
    exactly what a ``tail`` of the stream would show.
    """

    def __init__(self, maxlen: int = RING_BUFFER_LINES) -> None:
        super().__init__()
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._buf_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # logging must never raise
            self.handleError(record)
            return
        with self._buf_lock:
            self._lines.extend(text.splitlines() or [""])

    def lines(self, n: int | None = None) -> list[str]:
        """The last ``n`` lines (all if ``n`` is None), oldest first."""
        with self._buf_lock:
            items = list(self._lines)
        if n is None:
            return items
        return items[-n:] if n > 0 else []


_ring = RingBufferHandler()


def configure_logging(level: int | str = logging.INFO) -> None:
    """Install Inkframe's stream handler and ring buffer on the root logger.

    Idempotent: calling it again replaces our handlers instead of stacking
    them, and the ring buffer keeps its contents.  uvicorn is started with
    ``log_config=None`` so its loggers propagate here too.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _OWNED_ATTR, False):
            root.removeHandler(h)
    formatter = logging.Formatter(LOG_FORMAT)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    _ring.setFormatter(formatter)
    for h in (stream, _ring):
        setattr(h, _OWNED_ATTR, True)
        root.addHandler(h)
    root.setLevel(level.upper() if isinstance(level, str) else level)


def get_recent_logs(n: int = 200) -> list[str]:
    """The last ``n`` log lines recorded since ``configure_logging``, oldest first."""
    return _ring.lines(n)
