# Inkframe

A single-user, LAN-only e-ink photo frame. A Raspberry Pi Zero 2 W drives a
Waveshare 4inch e-Paper HAT+ (E) (E Ink Spectra 6: six colours, 600 x 400,
about 19 s per full refresh, no partial refresh). One Python process
(FastAPI, a scheduler thread and a one-thread render worker) serves a
no-build web UI and is the only thing that touches the panel. You upload
photos from a phone or desktop, pick the dithering method and adjustments that
look best on the six-colour panel, and decide what the frame cycles through:
photos, weather, headlines, generative patterns and quotes, with quiet hours.

There is no authentication. Keep it on a trusted LAN.

**Security.** Anyone who can reach the frame can use the UI and the API: the
LAN is the trust boundary. To keep other web pages out, a request that may
change something (any method but GET, HEAD and OPTIONS) gets a 403 unless its
`Host` names the frame (`localhost`, `inkframe`, a `*.local` name, a private,
loopback or link-local IP, the Pi's hostname, or a name in
`INKFRAME_ALLOWED_HOSTS`) and its `Origin`, if it has one, is that same host.
That refuses cross-site POSTs from a page you visit and DNS-rebinding hosts.
Request bodies, JSON included, are capped at 1 MiB (413), except photo
uploads, which have their own 25 MiB limit.

## Hardware

| Item | Value |
|---|---|
| Computer | Raspberry Pi Zero 2 W (512 MB RAM, about 415 MB usable), Raspberry Pi OS Lite 64-bit (Trixie, Python 3.13) |
| Panel | Waveshare 4inch e-Paper HAT+ (E), E Ink Spectra 6, controller frame 400 x 600 portrait |
| Bus | SPI mode 0, 4 MHz, `/dev/spidev0.0` (CE0 is the chip select) |
| Pins (BCM) | RST 17, DC 25, BUSY 24 (low = busy), PWR 18 (switches the HAT's 5 V rail) |
| Refresh | about 19 s, flickers, no partial refresh; operating range 0 to 50 C |

The driver is `inkframe/display/epd4in0e.py`, a trimmed port of the vendor
driver (kept for provenance in `vendor/waveshare/`, never imported). Every
BUSY wait has a timeout, and any failure powers the panel down.

### The six colours, and two corrections to the plan

The plan (`INKFRAME_PLAN.md` section 2) got two panel facts wrong. The code
follows the vendor driver, not the plan.

1. **Palette order and panel codes.** Inkframe's palette index is
   `0 black, 1 white, 2 yellow, 3 red, 4 blue, 5 green`. The controller's 4-bit
   codes for those are `0x0, 0x1, 0x2, 0x3, 0x5, 0x6`: it skips `0x4` (orange
   on the 7-colour Spectra panels). The plan's table (green 2, blue 3, red 4,
   yellow 5) must not be reintroduced. Source: vendor `EPD_4in0e.h` lines 43-48.
2. **Native orientation.** Apps and the pipeline work in landscape 600 x 400
   (`(400, 600)` index arrays), but the controller's own frame is portrait
   400 x 600, 200 bytes per row, 120000 bytes, high nibble = left pixel.
   `inkframe.display.palette.pack_frame` rotates 90 degrees counter-clockwise
   like the vendor `getbuffer`, so landscape pixel (599, 0) lands at native
   (0, 0). The `display.rotation` setting is 90 (the vendor orientation, the
   default) or 270 (frame mounted the other way up); the palette test card's
   arrow and "TOP LEFT" label make the mounting unambiguous. The Pi service
   has driven the real panel since Phase 7; this frame uses
   `display.rotation` 270.

Colours exist twice. **Nominal** is the pure primaries (what the vendor
driver assumes). **Measured** is what the panel actually shows (a grey-ish
white, dark blue, warm yellow, dull green). The shipped measured values are a
starting guess; tune them in Settings from a photo of the palette test card.
Photos dither against the measured palette by default, and every preview and
`last_frame.png` is drawn in it, so the browser looks like the panel.

## Install

You need a Pi flashed with Raspberry Pi OS Lite 64-bit (Trixie), reachable on
the LAN, with a login user named `inkframe` (it is also the service user) and
key-only SSH. On the Windows desktop you need Git, the OpenSSH client and
PowerShell 5.1, and an alias in `~/.ssh/config`:

```
Host inkframe
    HostName inkframe.local
    User inkframe
    IdentityFile ~/.ssh/inkframe
    IdentitiesOnly yes
```

If `inkframe.local` stops resolving (mDNS is flaky), put the Pi's IP in
`HostName`; every script uses the alias.

First bootstrap, from the repo root with `main` committed:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\bootstrap.ps1             # deploys main
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\bootstrap.ps1 -Snapshot   # deploys the working tree, uncommitted files included
```

It copies `install.sh` to the Pi and runs `bash ~/install.sh --bootstrap`
(apt packages, `spi`/`gpio` groups, SPI, `/opt/inkframe/{app,data}`, a bare
repo with a post-receive hook, the venv, the sudoers drop-in). If SPI had to be
enabled it reboots the Pi, waits for it to return and checks
`/dev/spidev0.0`. Then it runs `deploy.ps1 -SkipFrame`, whose first push
installs the systemd unit and starts the service. Re-running is safe. The
manual steps are in `docs/DEPLOY.md`.

The apt packages include `raspi-utils-core`, which provides the `pinctrl`
the unit uses (below). Python packages are installed from wheels only
(`pip install --only-binary=:all:`), so a pin without a wheel for the Pi
fails the install at once instead of compiling there.

**Sudo.** `install.sh` runs as `inkframe`, refuses to run as root, and uses
`sudo -n` for root steps so it never waits for a password. Bootstrap and full
installs need passwordless sudo, which Raspberry Pi OS gives the first user
(`/etc/sudoers.d/010_pi-nopasswd`). If that was removed, run once with a
cached password: `ssh -t inkframe "sudo -v && bash ~/install.sh --bootstrap"`.
Bootstrap also installs `/etc/sudoers.d/inkframe` (copy of
`deploy/sudoers-inkframe`) so the hook can restart the service without the
blanket rule. The service itself runs with `NoNewPrivileges` and cannot use sudo.

The unit file (`deploy/inkframe.service`) sets `INKFRAME_DISPLAY=epd`, so the
service drives the real panel. Set it to `mock` there to run the service on
the Pi without touching the panel; a changed unit file triggers a full update
on the next push. Before every start (`ExecStartPre`) and after every stop
(`ExecStopPost`) the unit runs `pinctrl set 17,18,25 op dl`, which drives
RST, PWR and DC low so the HAT's 5 V rail does not stay on after a crash.

### First time on the panel

1. Settings -> "Show test card on panel" (the Patterns app's variant 2). The
   test card has an arrow and a "TOP LEFT" label: if it hangs upside down,
   switch Panel rotation to the other value.
2. Photograph the panel in daylight and edit the six measured colours until the
   browser's swatches match what you see.
3. Look at a real photo and decide on `display.clear_before_refresh`
   (less ghosting, roughly twice as slow, off by default).

## Everyday deploy

Deploys are `git push` to the Pi's bare repo; a post-receive hook checks out
`main`, updates what changed, restarts the service and runs a health check.

```powershell
.\deploy.ps1               # push main, show /api/status, fetch data\pi_last_frame.png
.\deploy.ps1 -SkipFrame    # don't fetch the last frame
.\deploy.ps1 -Snapshot     # deploy the working tree as it is, without committing
.\deploy.ps1 -Force        # deploy main even with uncommitted changes (they are not pushed)
.\deploy.ps1 -Branch exp   # push local branch exp to the Pi's main
```

(Run PowerShell scripts as `powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1`.)
It refuses a dirty tree unless `-Snapshot` or `-Force`, refuses a commit
without `install.sh` and `deploy/post-receive-main`, and reads
`/opt/inkframe/last-deploy` afterwards because `git push` succeeds even when
the hook fails. A push that changes `requirements.txt`, `install.sh`,
`deploy/inkframe.service`, a C source or header under `inkframe/render/` or
`scripts/build_dither.sh` runs a full update (pip, native build, unit file,
restart); anything else is a restart plus health check. Details, recovery and
the sudoers list are in `docs/DEPLOY.md`.

## The web UI

Open `http://inkframe.local/` (or the Pi's IP; the dev server is
`http://localhost:8080/`). Hash-routed, mobile-first, no build step, nothing
from a CDN. Screenshots are in `docs/ui/`; the design notes are `docs/UI.md`.

| View | What it does |
|---|---|
| Now | The last frame exactly as the panel shows it, what is showing and what is next with a countdown, Show next, Pause / Resume, panel state, last refresh and keep-alive times. |
| Photos | Grid of the dithered renders, drop zone and file picker with per-file progress and browser-side downscale, portrait and excluded badges, Show / Exclude / Delete per photo. |
| Editor (`#/photos/<id>`) | Large preview, a strip of all 12 dither methods (Detail or Whole), sliders for strength, brightness, contrast, saturation, gamma and sharpen, serpentine and distance toggles, zoom, Save / Make default / Reset / Show on panel. |
| Rotation | Drag-to-reorder app slots with on/off and minutes, photos slot (minutes per photo, shuffle or sequential), cycle timeline, quiet hours, refresh floor, keep-alive, clear-before-refresh. |
| Apps | A form per app generated from its settings schema, with a preview image that never touches the panel. |
| Settings | Palette editor and test card, panel rotation, storage use, log tail, restart. |

## Photos, recipes and the render cache

```
upload -> ingest -> master (<= 1800 px JPEG q92, kept)
       -> fit (600 x 400, Lanczos, white bars; portrait is letterboxed, never rotated)
       -> adjust (brightness, contrast, saturation, gamma, sharpen; photo's box only)
       -> dither (always last, always 1:1) -> frame of palette indices
       -> cache  <data>/renders/<photo_id>/<key>.png
```

Dithering is always the last step at exactly 600 x 400: scaling a dithered
image destroys the pattern. Only the photo's own box is adjusted and dithered,
so the letterbox bars are exact palette white whatever the recipe.

- **Upload.** The browser downscales to 1800 px (JPEG q92) before sending;
  when it cannot decode a file (HEIC in Chrome) it sends the original and the
  server converts. The server sniffs magic bytes (JPEG, PNG, WebP, HEIC/HEIF,
  TIFF, BMP, GIF; AVIF is refused) and never trusts the name or
  `Content-Type`. Limits: 25 MiB, 60 MP, a 150 MB ingest memory budget checked
  before any pixel is decoded, two uploads holding bytes at once, 408 for a
  body that stalls. Originals are not kept. Details and the measured memory
  table: `docs/PIPELINE.md`.
- **Recipe.** `method`, `serpentine`, `strength` (0.5-1.0), `distance`
  (`rgb`/`lab`), `brightness`, `contrast`, `saturation` (0.5-1.5), `gamma`
  (0.5-2.0), `sharpen` (0-2), `palette` (`nominal`/`measured`; with
  `palette.use_measured` off every photo uses `nominal`). The default
  recipe (`settings.default_recipe`) is atkinson, serpentine, strength 0.85,
  lab, saturation 1.1, measured palette, the rest neutral. A photo either
  follows the default or stores a complete override; "Make this the default"
  changes every photo that follows it. If the photo on the panel changes
  recipe, the next rotation change repaints it instead of skipping it as
  already showing.
- **Methods.** None, Floyd-Steinberg, Atkinson, Jarvis, Stucki, Burkes, Sierra,
  Sierra two-row, Sierra Lite, Bayer 4x4, Bayer 8x8, blue noise. The engine
  is C, integer-only, bit-identical between MSVC/x64 and gcc/aarch64
  (`docs/DITHER.md`).
- **Cache.** A render key is a sha256 prefix over the recipe, both palettes,
  `pipeline.FORMAT_VERSION` and the engine's ABI plus a digest of `dither.c`
  and the blue-noise texture, so any change gives a new key and stale files
  are simply never asked for. Renders are 4-bit paletted PNGs whose pixel
  values are the frame; at most 40 per photo are kept; URLs carry the key and
  are cached forever by the browser.

## Apps

Each app renders one 600 x 400 card in the six palette colours directly (flat
fills, thresholded type, no gradients), except Photos, which returns the
cached dithered render. All have layout variants, cache what they fetch, and
degrade to a cached or "offline" card.

| App | What | Settings |
|---|---|---|
| Photos | Your uploaded photos, with their recipes. A "No photos yet" card when there are none. | Per slot: `dwell_minutes` (default 3), `order` (`shuffle` default, or `sequential`). Shuffle avoids the last `min(n - 1, 20)` photos. |
| Weather | Open-Meteo: now, next hours, five days; three layouts. Forecast cached 15 min, geocoding cached forever. | `location` (place name or `lat,lon`; required), `units` (`metric`/`imperial`; imperial also uses a 12-hour clock), `layout` (`rotate` = a different one each hour, `now`, `days`, `poster`), `label`. |
| Headlines | RSS/Atom top stories set like a front page; three layouts. Feeds fetched in parallel (9.5 s cap), cached 15 min; stale data is shown with a red "offline" note. | `feeds` (one URL per line, optional short name before it; default BBC, NYT, Guardian), `count` (3-8), `max_age_hours` (1-72), `mix` (`interleave`/`by_feed`). |
| Patterns | Generative prints: Truchet, Bauhaus, halftone, flow field, concentric, plus the palette test card. No network. | `family` (`random`, the five, or `test_card`), `density` (1-5), `palette_mood` (`full`, `duotone`, `warm`, `cool`), `caption`. |
| Quotes | One quotation per refresh, three layouts. 311 bundled public-domain quotes work offline; optional ZenQuotes (`https://zenquotes.io/api/random`) with the attribution their terms require, falling back to the bundled set. | `source` (`local`/`zenquotes`), `tags` (wisdom, nature, courage, humour, love, work, time, art, science, stoic), `length` (`short`/`any`/`long`), `show_source`. |

Per-app settings live in the database (`app_settings`), are validated against
the app's schema (text and list fields have length and count caps), and are
edited in the Apps view or with `PUT /api/apps/{id}/settings`.

## The scheduler and the panel-care rules

`inkframe/scheduler.py` is the only code that drives the panel, and it
enforces the vendor's care rules in code, whatever the settings say. Design
notes, worked timelines and the full decision order: `docs/SCHEDULER.md`.

- **Refresh floor.** Two refreshes are never closer than
  `panel.refresh_floor_seconds` (default 180 s, never below 30), measured from
  the end of the last attempt on both the wall and the monotonic clock. A
  failed attempt counts.
- **Show-now minimum.** "Show now", "Show next" and the Photos / Apps "show"
  buttons are the only things that bypass the floor, and they still wait
  `panel.show_now_min_seconds` (default 30) after the last attempt. One request
  is pending at a time; a newer one replaces it.
- **Keep-alive.** The panel is refreshed at least every `panel.keepalive_hours`
  (default 20, at most 23) even while paused or in quiet hours, by re-showing
  the last frame byte for byte. The floor may not exceed the keep-alive interval.
- **Quiet hours.** No scheduled rotation refreshes between `start` and `end`
  (default 23:00 to 07:00, every day); the window may cross midnight and `days`
  are the weekdays a window starts on (Mon = 0). Keep-alive and show-now still work.
- **Power.** Every refresh is `init()`, optional `clear()`, `show()`, with
  `sleep()` in a `finally`. A refresh in flight finishes on shutdown.
- **Robustness.** A render that raises or returns an invalid frame is logged
  and its slot skipped before the panel is touched; if every slot fails the
  scheduler backs off 60 s. A display failure counts as an attempt and the
  panel is repainted at the next rotation change. State (rotation position,
  timing anchors, recent photos, the last frame) survives a restart.
  Clock jumps (no RTC on the Pi; NTP steps after boot) cannot shorten the floor.

## Configuration

**`<data>/settings.json`** is created with defaults on first start. An invalid
file is moved aside to `settings.json.invalid` and defaults are used. It is
the same object as `GET/PUT /api/settings`; a PUT deep-merges (lists such as
`rotation` and `quiet_hours.days` are replaced whole) and unknown keys are a 422.

| Key | Default | Meaning |
|---|---|---|
| `display.rotation` | `90` | Packing rotation, 90 or 270 (see above). |
| `display.clear_before_refresh` | `false` | Flood the panel white before each refresh. |
| `panel.refresh_floor_seconds` | `180` | 30 to 86400, at most `keepalive_hours` x 3600. |
| `panel.show_now_min_seconds` | `30` | 30 to the floor. API/file only; the UI does not edit it. |
| `panel.keepalive_hours` | `20` | 1 to 23. |
| `quiet_hours.{enabled,start,end,days}` | `true`, `"23:00"`, `"07:00"`, all days | `HH:MM` 24-hour, start and end must differ. |
| `palette.measured` | see `inkframe/display/palette.py` | The six `[r, g, b]` colours the panel shows. |
| `palette.use_measured` | `true` | Global switch: when off, every photo renders against the nominal primaries whatever its recipe says. |
| `default_recipe` | atkinson / 0.85 / lab / measured | The recipe photos follow. |
| `rotation` | photos 10 min (3 per photo, shuffle), weather 3, headlines 3, quotes 2, patterns 2 | Up to 32 slots of `{app_id, enabled, duration_minutes (1-1440), options}`. |
| `paused` | `false` | Rotation paused (keep-alive still runs). |

**Environment variables**

| Variable | Default | Meaning |
|---|---|---|
| `INKFRAME_DISPLAY` | `mock` | `mock` or `epd` (the real panel; Pi only). The systemd unit sets `epd`. |
| `INKFRAME_DATA` | `<repo>/data` | Data directory (`/opt/inkframe/data` on the Pi). |
| `INKFRAME_HOST` | `0.0.0.0` | Bind address. |
| `INKFRAME_PORT` | `8080` | Port; the unit uses 80 through `CAP_NET_BIND_SERVICE`. |
| `INKFRAME_SCHEDULER` | `on` | `off` (or `0`, `false`, `no`) builds the scheduler but does not start its thread. The tests set it. |
| `INKFRAME_OFFLINE` | unset | `1` (or `true`, `yes`, `on`) forbids network access in every scheduler and preview render. The tests set it. |
| `INKFRAME_ALLOWED_HOSTS` | unset | Extra host names the Host/Origin check accepts, comma-separated (`name`, `name:port` or an IP), besides `localhost`, `inkframe`, `*.local`, private IPs and the machine's hostname (see Security above). |

**HTTP API.** Errors are always `{"error": "<message>"}`. Ids are 32 lowercase
hex characters. `/docs` is FastAPI's Swagger page and needs internet for its assets.

| Route | |
|---|---|
| `GET /api/health`, `GET /api/status` | liveness; everything the Now view shows (`version`, `display`, `panel`, `rotation`, `settings`, `system` incl. storage) |
| `GET /api/system/logs?n=200`, `POST /api/system/restart` | last log lines (ring of 500); stop the process so systemd restarts it |
| `GET/PUT /api/settings` | the settings tree |
| `GET /api/display/frame.png`, `POST /api/display/next\|pause\|resume\|show` | the panel's last frame; scheduler controls (`show` takes `{app_id[, variant]}` or `{photo_id}`) |
| `GET/PUT /api/rotation` | slots (validated more strictly than `/api/settings`) |
| `GET /api/apps`, `GET/PUT /api/apps/{id}/settings`, `GET /api/apps/{id}/preview.png?variant=&seed=` | apps, their forms and previews |
| `GET/POST /api/photos`, `GET/DELETE /api/photos/{id}`, `GET /api/photos/{id}/render[?recipe=<key>]`, `POST /api/photos/{id}/previews`, `PUT /api/photos/{id}/recipe`, `PUT /api/photos/{id}/flags` | photos |
| `GET/PUT /api/recipe/default` | default recipe plus the method registry and knob limits |

## Where things live

**On the Pi**

| Path | What |
|---|---|
| `/opt/inkframe/repo.git` | bare repo, the push target (`git remote pi`); `deploy.lock` serialises deploys |
| `/opt/inkframe/app` | checkout of `main`, the service's working directory |
| `/opt/inkframe/.venv` | venv with `--system-site-packages` (spidev, gpiozero, lgpio come from apt) |
| `/opt/inkframe/data` | everything below; deploys never touch it |
| `/opt/inkframe/last-deploy` | `<sha> running\|ok\|failed <utc time>` |
| `/etc/systemd/system/inkframe.service`, `/etc/sudoers.d/inkframe` | copies of `deploy/inkframe.service`, `deploy/sudoers-inkframe` |

**In the data directory:** `settings.json`, `inkframe.db` (SQLite, WAL: photos,
per-app settings, scheduler state), `masters/<id>.jpg`, `renders/<id>/<key>.png`,
`cache/<app_id>/` (network caches), `last_frame.png` (what the panel shows, in
measured colours) and `last_frame.bin` (the exact 120000 controller bytes).

## Logs and recovery

If the frame keeps dropping off Wi-Fi, run `ssh inkframe "bash /opt/inkframe/app/install.sh --system"` once: it turns the Wi-Fi chip's power saving off, installs a gateway-ping watchdog that bounces the radio (and reboots after 15 minutes without a gateway) and makes the journal persistent. Details in `docs/DEPLOY.md`.

```powershell
ssh inkframe "journalctl -u inkframe -n 100 -f"        # add sudo if it shows nothing
ssh inkframe "systemctl status inkframe"
ssh inkframe "curl -s http://127.0.0.1/api/status"
ssh inkframe "sudo systemctl restart inkframe"
ssh inkframe "bash /opt/inkframe/app/install.sh --health"
```

The Settings view shows the in-memory log tail, and "Restart" calls
`POST /api/system/restart`. If a deploy fails, the hook prints the last 40
journal lines, records `failed`, and starts the service again if it is down.
To repair an install run `ssh inkframe "bash /opt/inkframe/app/install.sh"`
(or copy a fresh `install.sh` over first). To re-run the last deploy without a
new commit, run `post-receive-main` by hand (`docs/DEPLOY.md`, Recovery).
If the process is killed mid-refresh, the unit's `ExecStopPost` drives RST,
PWR and DC low so the HAT's 5 V rail does not stay on (`ExecStartPre` does
the same before every start). `install.sh` exit codes: 0 ok, 1 failure,
2 usage, 3 finished but a reboot is required.

## Development

Everything runs on the Windows desktop against the mock display, which writes
`data/last_frame.png` and the exact `data/last_frame.bin`. Use the venv's
Python: `python3` on the desktop is a broken Store stub, and the Pi's Python
is 3.13 while the desktop's is 3.10, so code must run on both.

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -U pip
.venv/Scripts/python -m pip install -r requirements-dev.txt

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_dither.ps1   # MSVC -> inkframe\render\dither.dll
.venv/Scripts/python -m pytest -q                  # over 1000 tests, about 90 s
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m inkframe                   # http://localhost:8080/
```

**Native dither library.** Dithering runs in C (`inkframe/render/dither.c`,
loaded with ctypes). Build it once before the tests and again after changing
the C file. The script finds Visual Studio 2022 with `vswhere` and needs the
C++ workload. On Linux `bash scripts/build_dither.sh` builds
`inkframe/render/libdither.so` with gcc. The Pi builds `libdither.so` itself
during a full update, so nothing native is ever pushed; both outputs are
gitignored. There is no Python fallback: without the library
`inkframe.render.dither` raises `DitherLibraryMissing` naming the build
command, `tests/test_dither.py` fails every dither test with it, and the rest
of the suite still runs. For experiments:
`python -m inkframe.render.dither IN.png OUT.png --method atkinson --fit`.

**Rules.** Line endings are LF everywhere (`.gitattributes`), except `*.ps1`
(CRLF). Runtime dependencies are exact pins in `requirements.txt`, mirrored in
`pyproject.toml`; every pin and its dependencies ship wheels for Python 3.13 on
aarch64 and the Pi installs them with `--only-binary=:all:`, so it compiles
nothing but `dither.c`. Do not add dependencies. Ruff (line length 100,
rules E, F, W, I, B, UP) must be clean, and every module starts with
`from __future__ import annotations`.

**Tests on the Pi** need the service stopped (`sudo systemctl stop inkframe`)
or they can run the 415 MB Pi out of memory.

### Test layout

| Files | Cover |
|---|---|
| `test_palette.py`, `test_epd4in0e.py`, `test_mock_display.py` | palette, packing round trip, wire-level driver tests against fake SPI/GPIO, mock display |
| `test_dither.py`, `dither_reference.py`, `golden/dither_golden.json` | the C engine against a pure-Python spec, properties, 120 golden hashes shared by MSVC and gcc |
| `test_masters.py`, `test_masters_memory.py`, `test_adjust.py`, `test_pipeline.py` | ingest, hardening and measured memory peaks, adjust, fit/recipe/keys/cache |
| `test_api*.py`, `test_db.py`, `test_config.py` | HTTP routes, SQLite, settings |
| `test_apps_base.py`, `test_app_{photos,weather,headlines,patterns,quotes}.py` | the app contract and each app (fixtures in `fixtures/`, never the network) |
| `test_scheduler.py`, `test_quiet_hours.py`, `test_scheduler_probe.py` | fake-clock tests of every panel-care rule; `conftest.py` holds `FakeClock`, `Rig`, `FakeApp`, `RecordingDisplay` |
| `test_static.py` | UI size budget (300 KB) and that every JS import resolves |

`conftest.py` sets `INKFRAME_SCHEDULER=off` and `INKFRAME_OFFLINE=1` for every test.

## Documentation

| File | |
|---|---|
| `docs/DEPLOY.md` | deploy runbook: bootstrap, hook, recovery, sudo |
| `docs/SCHEDULER.md` | decision order, panel-care rules, clock safety, state |
| `docs/PIPELINE.md` | ingest, fit/adjust/dither, recipes, cache, limits, memory |
| `docs/DITHER.md` | fixed-point engine, the 12 methods, accuracy, timings |
| `docs/UI.md` | views, routing, design |
| `docs/ROUGH_EDGES.md` | known rough edges still open, and what needs the owner or the hardware |
| `docs/previews/`, `docs/ui/` | app card and UI screenshots |
| `CHANGELOG.md`, `CLAUDE.md` | history; architecture notes for Claude Code sessions |
| `INKFRAME_PLAN.md` | the original build plan (outside the repo) |

## Licence and third-party notices

Inkframe itself has no licence yet: the owner has not chosen one, so there
is no licence file.

- **Waveshare driver.** `vendor/waveshare/` holds verbatim copies of the
  Waveshare e-Paper driver files (github.com/waveshare/e-Paper, commit
  `a794fbc`). The upstream repository has no LICENSE file; each source file
  carries an inline MIT-style permission notice, preserved unchanged, which
  covers those files and the derived port in `inkframe/display/epd4in0e.py`.
- **Fonts.** Playfair Display 1.202, Inter 4.1 and JetBrains Mono 2.304, all
  SIL Open Font License 1.1, unmodified, in `inkframe/fonts/` with each
  family's `OFL.txt` (index and sources in `inkframe/fonts/README.md`).
- **Quotes.** `inkframe/apps/data/quotes.json` holds 311 public-domain quotations
  (authors who died before 1928 or classical sources in older public-domain translations).
- **ZenQuotes.** When enabled, quotes come from zenquotes.io; every such card
  carries their required attribution, "Quotes by ZenQuotes.io".
- **Open-Meteo.** Weather and geocoding come from open-meteo.com's free API,
  whose data is offered under CC BY 4.0; credit it if you share screenshots.
- **Feeds.** Headlines shows story titles from the feeds you configure
  (BBC, NYT and Guardian by default); the content belongs to the publishers.
  The test fixtures under `tests/fixtures/` contain trimmed real feed and
  forecast responses.
- **Python packages.** FastAPI, Starlette, uvicorn, python-multipart, pydantic,
  NumPy, Pillow, pillow-heif (libheif), httpx and feedparser, pinned in
  `requirements.txt`; dev tools are pytest, pytest-timeout and ruff.
