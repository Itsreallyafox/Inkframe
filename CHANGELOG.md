# Changelog

All notable changes to Inkframe. One entry per build phase; dates are UTC.

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
