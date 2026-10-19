# Changelog

All notable changes to Inkframe. One entry per build phase; dates are UTC.

## 0.1.0 — Phase 6: scheduler (2026-10-19)

- `inkframe.scheduler.Scheduler`, the only thing that drives the panel. It
  enforces the panel-care rules in code:
  - the refresh floor, measured from the end of the last attempt (failed
    attempts count);
  - show-now, the one bypass of the floor, which still waits
    `show_now_min_seconds`; one pending request, a newer one replaces it, and
    `fires_at` is reported for the UI countdown;
  - a keep-alive that re-shows the last frame byte for byte while paused or in
    quiet hours;
  - `init -> [clear] -> show -> sleep` with `sleep()` in a `finally`;
  - quiet hours, as windows that start on the listed weekdays and may cross
    midnight;
  - render failures skipped slot by slot, with a 60 s backoff when every slot
    fails. A timed-out render is cancelled, or, while it still runs, later
    renders fail at once instead of queueing behind it. A photo list that
    cannot be read fails the photos slot the same way instead of stopping
    every refresh, the keep-alive included;
  - display failures logged and counted as attempts. The last good frame is
    written back to disk after a failed refresh, and the panel counts as
    stale, so the next rotation change repaints even the same photo;
  - every rendered frame validated before the panel is touched (an invalid
    frame from the dither path is a render failure, not a panel failure),
    and a render raising `SystemExit` skipped like any other failure instead
    of ending the scheduler thread.

  Its decision core `tick(now)` returns the next delay. It runs on a daemon
  thread that `wake()` interrupts.
- Photos slot: dwell (default 3 min), shuffle (avoids the last
  `min(n - 1, 20)`) or sequential (`created_at` order), and a "no photos" card
  that lasts one dwell. A photo change that would push the slot past its end
  is skipped, and an identical photo is never re-shown.
- State survives restart. It is stored under the db key `scheduler`, mirrored
  to `last_refresh_at`/`last_source`, with the frame in
  `last_frame.bin`/`.png`. That covers the rotation position, timing anchors,
  recent photos and the frame to keep alive. Unreadable state, even a row
  that is not JSON, gives defaults instead of stopping the boot, and
  `/api/status` reports it as unknown instead of failing. A lost attempt
  anchor falls back to the last refresh, so the floor survives it.
- Settings: `panel.refresh_floor_seconds` may not exceed
  `panel.keepalive_hours`, since a longer floor would hold the keep-alive
  back.
- Shutdown waits up to 100 s for a refresh in flight (not for a render), and
  leaves the display to a refresh that is still running; the systemd unit's
  `TimeoutStopSec` is now 150 s. `Scheduler.invalidate()` lets Phase 3
  repaint a photo whose recipe changed.
- Clock safety for a Pi without an RTC. The floor and the show-now minimum are
  measured on the wall clock and the monotonic clock, so no clock step
  shortens them. Wall-clock jumps are detected against the monotonic clock and
  the timers shifted. Anchors loaded from disk move only in their safe
  direction, so NTP's first step after a power cut never postpones the
  keep-alive. Anchors found in the future after a restart are clamped. A
  DST-aware `now_fn` is handled at its offset of the moment.
- API: `POST /api/display/next|pause|resume|show`, `GET/PUT /api/rotation`,
  `GET /api/apps`, `GET/PUT /api/apps/{id}/settings` and
  `GET /api/apps/{id}/preview.png`. Previews run on the render executor, never
  the panel. `/api/status` keeps its Phase 1 keys and adds
  `panel.last_attempt_at`, `panel.last_error`, `rotation.quiet`,
  `rotation.quiet_until` and `rotation.pending`; it also fills `current`,
  `next`, `next_at` and `keepalive_at`.
- `create_app(scheduler=False)` and `INKFRAME_SCHEDULER=off` build the
  scheduler without starting its thread. `INKFRAME_OFFLINE=1` makes scheduler
  and preview renders offline. Shutdown stops the scheduler before the
  display. An app that fails to import is logged and skipped instead of
  stopping the frame from booting.
- `inkframe/apps/photos.py`: a stub photos app (a "no photos" card and a
  placeholder per photo) that Phase 3 replaces.
- Fake-clock tests for every rule (`tests/test_scheduler.py`,
  `tests/test_quiet_hours.py`) and API tests (`tests/test_api_display.py`,
  `tests/test_api_apps.py`). Design notes are in `docs/SCHEDULER.md`.
## 0.1.0 — Phase 2: dither engine (2026-10-19)

- `inkframe/render/dither.c`: all 12 methods (nearest, 8 error-diffusion
  kernels with serpentine and strength, Bayer 4/8, 64x64 blue noise) in
  integer-only fixed point with an integer CIE76 Lab path, so output is
  bit-identical between MSVC/x64 and gcc/aarch64 (120 shared golden hashes).
  Ordered methods derive their threshold spread from the palette so flat
  palette colours never speckle. Worst case 104 ms per frame on the Pi.
- `inkframe/render/dither.py`: ctypes loader with ABI check, method
  registry with UI names, validation, a small CLI.
- Build scripts for the Pi (gcc) and the desktop (MSVC via vcvars64);
  `install.sh` builds `libdither.so` on every push that touches the C.
- Pure-Python reference implementation and 177 tests, including a
  mutation-tested exact-match suite; `docs/DITHER.md`.

## 0.1.0 — Phase 1: scaffold and display layer (2026-10-19)

- Repository layout, LF normalisation, Python 3.10/3.13-compatible package.
- `inkframe.display.palette`: the six-colour palette with the panel's real
  4-bit codes (black 0, white 1, yellow 2, red 3, blue 5, green 6), nominal and
  measured RGB sets, and `pack_frame`/`unpack_frame` that reproduce the vendor
  driver's portrait packing byte for byte.
- `inkframe.display.base`: the `Display` protocol (init/show/clear/sleep/close,
  awake/state) that the scheduler, mock and real driver share.
- `inkframe.display.epd4in0e`: trimmed port of the Waveshare driver with
  injectable SPI/GPIO backends, BUSY timeouts, and a power-down path on every
  failure. Originals kept under `vendor/waveshare/` for provenance.
- `inkframe.display.mock`: desktop stand-in that writes `last_frame.png` and
  the exact `last_frame.bin` the panel would receive.
- Settings (`config.py`, JSON on disk, validated dataclasses), SQLite layer
  (`db.py`, versioned migrations), FastAPI app with `/api/status`,
  `/api/health`, `/api/settings`, `/api/system/logs`, and a placeholder page.
- `install.sh` (`--bootstrap`, `--update`, `--health`, full), systemd unit
  (port 80 via `CAP_NET_BIND_SERVICE`, PWR-low safety net on stop), a tiny
  bare-repo `post-receive` hook that hands off to the versioned
  `deploy/post-receive-main`, `deploy.ps1` (`-Snapshot` deploys an uncommitted
  working tree) and `deploy/bootstrap.ps1`.
- Pi bootstrapped: SPI enabled, service running with `INKFRAME_DISPLAY=mock`.
