# Changelog

All notable changes to Inkframe. One entry per build phase; dates are UTC.

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
