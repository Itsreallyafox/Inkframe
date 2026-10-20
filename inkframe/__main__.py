"""``python -m inkframe``: serve the app with uvicorn.

``INKFRAME_HOST`` (default ``0.0.0.0``) and ``INKFRAME_PORT`` (default 8080;
the systemd unit sets 80) choose the socket.  uvicorn gets ``log_config=None``
so ``inkframe.log`` stays in charge of formatting.
"""

from __future__ import annotations

import os

import uvicorn

from inkframe.log import configure_logging

#: how long uvicorn waits for open requests on SIGTERM before cancelling them
GRACEFUL_SHUTDOWN_SECONDS = 30


def main() -> None:
    """Parse the environment and run uvicorn until SIGINT/SIGTERM."""
    host = os.environ.get("INKFRAME_HOST", "").strip() or "0.0.0.0"
    raw_port = os.environ.get("INKFRAME_PORT", "").strip() or "8080"
    try:
        port = int(raw_port)
    except ValueError:
        port = -1
    if not 0 < port < 65536:
        raise SystemExit(f"INKFRAME_PORT must be a TCP port number, got {raw_port!r}")
    # configure before uvicorn starts so its start-up lines are formatted too
    configure_logging()
    uvicorn.run(
        "inkframe.app:create_app",
        factory=True,
        host=host,
        port=port,
        log_config=None,
        access_log=False,
        # on SIGTERM uvicorn drains open requests before the lifespan shutdown
        # that stops the scheduler; without a bound a stalled upload (up to
        # UPLOAD_READ_SECONDS = 300 s) would hold it past the unit's
        # TimeoutStopSec=150 and get the process SIGKILLed mid-refresh.
        # Budget: 30 s drain + 100 s scheduler stop (SCHEDULER_STOP_TIMEOUT)
        # < 150 s.
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
