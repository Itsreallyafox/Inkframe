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
Pi never compiles anything.

Install, deployment and usage documentation will follow as the remaining
phases land.
