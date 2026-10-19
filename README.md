# Inkframe

A single-user, LAN-only e-ink photo frame: a Raspberry Pi Zero 2 W driving a
Waveshare 4inch e-Paper HAT+ (E) (E Ink Spectra 6: six colours, 600 x 400,
about 19 s per full refresh). One Python process (FastAPI, a scheduler thread
and a render worker) serves a no-build web UI and owns the panel.

There is no authentication. Keep it on a trusted LAN.

## Dev loop (Windows desktop, Python 3.10)

Everything runs on the desktop against a mock display that writes
`data/last_frame.png` (what the panel would show) and `data/last_frame.bin`
(the exact 120000 bytes the driver would send).

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -U pip
.venv/Scripts/python -m pip install -r requirements-dev.txt

.venv/Scripts/python -m pytest -q        # tests
.venv/Scripts/python -m inkframe         # http://localhost:8080/
```

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `INKFRAME_DISPLAY` | `mock` | `mock` or `epd` (the real panel; Pi only) |
| `INKFRAME_DATA` | `<repo>/data` | data directory: settings, database, photos, caches |
| `INKFRAME_HOST` | `0.0.0.0` | bind address |
| `INKFRAME_PORT` | `8080` | port (the Pi's systemd unit uses 80) |

Useful endpoints: `GET /api/status`, `GET/PUT /api/settings`,
`GET /api/system/logs?n=200`, `GET /api/health`.

## Native dither library

Dithering runs in C (`inkframe/render/dither.c`, loaded with ctypes). Build
it once on the desktop before running the tests, and again after changing the
C file:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_dither.ps1   # MSVC -> inkframe\render\dither.dll
```

The script finds Visual Studio 2022 with `vswhere` and needs the C++
workload. On Linux, `bash scripts/build_dither.sh` builds
`inkframe/render/libdither.so` with gcc. The Pi builds `libdither.so` itself
on every deploy that touches `inkframe/render/*.c` (`install.sh`
`build_native`), so nothing native is ever pushed. Both outputs are
gitignored.

There is no Python fallback. Without the library, `inkframe.render.dither`
raises `DitherLibraryMissing` naming the build command, and
`tests/test_dither.py` fails every dither test with the build command (the rest of the suite still runs). For experiments:
`python -m inkframe.render.dither IN.png OUT.png --method atkinson --fit`.
Design notes, fixed-point rules and timings are in `docs/DITHER.md`.

## Layout

```
inkframe/          the application package (run with python -m inkframe)
  display/         palette + packing, Display protocol, mock and panel drivers
  api/             JSON routes
  static/          the web UI (plain ES modules + CSS)
tests/             pytest suite
```

Runtime dependencies are pinned in `requirements.txt` (mirrored in
`pyproject.toml`); every compiled one has a Python 3.13 aarch64 wheel so the
Pi never compiles anything except our own `dither.c`.

Install, deployment and usage documentation will follow as the remaining
phases land.
