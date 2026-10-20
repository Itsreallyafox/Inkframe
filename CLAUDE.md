# CLAUDE.md

Inkframe: a LAN-only, single-user, six-colour e-ink photo frame. One Python process on a
Raspberry Pi Zero 2 W (FastAPI + a scheduler thread + a one-thread render worker) serves a
no-build web UI and is the only thing that drives a Waveshare 4inch e-Paper HAT+ (E)
(600 x 400, ~19 s full refresh, no partial refresh). No auth, by design. README.md is the
user guide; docs/ holds design notes (DEPLOY, SCHEDULER, PIPELINE, DITHER, UI) and ROUGH_EDGES.md
(what is still open); INKFRAME_PLAN.md (outside the repo, `C:\Users\Itsre\Desktop\`)
is the original plan and is wrong in places (below).

## Run, test, deploy

- Desktop is Windows, Python 3.10. Always `C:/Users/Itsre/Desktop/Inkframe/.venv/Scripts/python`
  (`python3` is a broken Store stub; plain `python` may not be the venv). Shell is Git Bash.
- Run: `python -m inkframe` -> http://localhost:8080/ with the mock display (writes
  `data/last_frame.png` and `.bin`). Tests: `python -m pytest -q` (1000+ tests, ~90 s;
  pass `-p no:cacheprovider` in worktrees). Lint: `python -m ruff check .` and `ruff format --check .`.
- Build the dither DLL first (MSVC): `powershell -NoProfile -ExecutionPolicy Bypass -File
  scripts\build_dither.ps1`. Rebuild after any `dither.c` change. Pi builds `libdither.so` itself.
- Deploy is `git push`, via `powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1`
  (`-Snapshot` ships the uncommitted tree; `-SkipFrame`, `-Force`, `-Branch`). First install:
  `deploy\bootstrap.ps1`. Details: docs/DEPLOY.md. Agents do not commit unless told to.
- Python 3.13 (the Pi) is never run on the desktop: write 3.10-and-3.13 code (no `tomllib`,
  `Self`, `StrEnum`, `datetime.UTC`, PEP 695). Every module starts `from __future__ import annotations`.

## Pi facts

- `ssh inkframe` (alias: user `inkframe`, key auth, `HostName inkframe.local`). mDNS is flaky: if
  it fails to resolve use `-o HostName=<ip> -o HostKeyAlias=inkframe.local` (IP was 10.0.1.238, DHCP).
  Never ask Adam for a password. Do not touch the Pi or the panel unless the task says so.
- Raspberry Pi OS Lite 64-bit Trixie, Python 3.13.5, gcc 14, ~415 MB usable RAM (service RSS 80-115 MB).
  Running the whole pytest suite beside the service can OOM and reboot it: stop the service first.
- Paths: `/opt/inkframe/{repo.git, app, .venv, data, last-deploy}`; service `inkframe` (systemd,
  port 80 via CAP_NET_BIND_SERVICE, `SupplementaryGroups=spi gpio`, TimeoutStopSec=150,
  ExecStartPre and ExecStopPost `-/usr/bin/pinctrl set 17,18,25 op dl`; pinctrl comes from the apt
  package raspi-utils-core and works for the service user). The unit sets `INKFRAME_DISPLAY=epd`
  (since commit 1628e1a); set `mock` there to run on the Pi without touching the panel.
- Phase 7 (hardware bring-up) is done (2026-10-19): the real `SpidevBus`/`GpiozeroGpio` backends
  drive the panel, the first real refresh took 19.1 s, the test card confirmed the six colours
  (panel codes) and the orientation. This frame hangs the other way up: its `settings.json` has `display.rotation` 270
  (the code default stays 90; do not change the default).
- sudo: install.sh refuses root and uses `sudo -n`; passwordless sudo comes from Pi OS's
  `010_pi-nopasswd` plus `/etc/sudoers.d/inkframe` (identical to `deploy/sudoers-inkframe`). The
  service has NoNewPrivileges and cannot sudo. Journal: `ssh inkframe "journalctl -u inkframe -n 100"`.

## Architecture map (inkframe/)

- `__main__.py` `main()`: INKFRAME_HOST/PORT -> `uvicorn.run("inkframe.app:create_app", factory=True,
  timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS)` (30 s drain on SIGTERM, then the lifespan).
- `app.py` `create_app(data_dir, display, display_kind, scheduler=True)`: lifespan builds `app.state`
  {paths, settings, db, display, display_env, render_executor, apps, fonts, scheduler, started_*};
  shutdown = `scheduler.stop(100)`, then `display.close()` (skipped while `scheduler.refreshing`), executor, db.
  Also the Host/Origin check, the `{"error"}` 500/404/422 handlers and the `/static` (no-cache)
  and `/fonts` mounts.
- `config.py`: frozen, self-validating `Settings` tree (`DisplaySettings`, `PanelSettings`, `QuietHours`,
  `PaletteSettings`, `RotationSlot`), `DEFAULT_RECIPE`, `RECIPE_RANGES`, `validate_recipe`, `SettingsStore`
  (`get` lock-free; `replace/update/patch` validate, save atomically, swap). Bad file -> `settings.json.invalid`.
- `db.py` `Database` (sqlite3, WAL, one RLock): `Photo`, `add/get/list/delete_photo`, `set_photo_recipe/excluded`,
  `get/set_app_settings`, `get/set_state`; `MIGRATIONS` is append-only. `paths.py`: `DataPaths`,
  `resolve_data_dir`, `atomic_write_bytes`. `log.py`: `configure_logging`, `get_recent_logs` (500-line ring).
- `scheduler.py` `Scheduler`: `tick(now)` (one decision, at most one refresh, returns next delay),
  `start/stop/wake`, `pause/resume`, `show_now(app_id|photo_id, variant)`, `show_next`, `invalidate`
  (called by `PUT /api/photos/{id}/recipe` with the id and by `PUT /api/recipe/default` without one),
  `snapshot`; helpers `quiet_window_active`, `pick_next_photo`, `photo_options`, `build_render_context`.
- `display/palette.py`: indices, `PANEL_CODES`, `NOMINAL`/`MEASURED_DEFAULT`, `pack_frame/unpack_frame`,
  `indices_to_image`, `rgb_to_indices_exact`. `display/base.py`: `Display` protocol, `validate_frame`,
  `DisplayError/Timeout/NotReady`. `display/epd4in0e.py` `EPD4in0e` (real panel; injectable `SpiBus`/`Gpio`),
  `mock.py` `MockDisplay`, `factory.py` `create_display(kind, ...)` (imports the driver lazily).
- `render/dither.py` (+ `dither.c`, `blue_noise_64.npy`): ctypes wrapper, `METHODS` registry (12), `dither()`.
  `masters.py` `ingest()` (sniff, bomb/memory guards, master JPEG). `pipeline.py`: `fit_image`, `render_frame`,
  `complete_recipe`, `effective_recipe`, `recipe_key`, `FORMAT_VERSION`. `adjust.py` `apply`. `cache.py`
  `RenderCache` (`render_or_get`, `render_methods`, `prune`, `invalidate`).
- `apps/base.py`: `App` protocol, `Field`, `validate_settings`, `RenderContext`, `FontLibrary`,
  `render_to_frame`, `AppError`. `apps/__init__.py`: `APP_IDS`, `load_app`. `apps/draw.py` `Canvas`
  (palette-only drawing, thresholded text). `apps/photos.py` (serves cached renders); `weather*.py`
  (Open-Meteo), `headlines*.py` (feedparser), `patterns*.py` (6 families + test card), `quotes*.py`
  (`data/quotes.json`, ZenQuotes). `fonts/` holds the OFL fonts + `fonts.json`.
- `api/`: `__init__` (Depends accessors, `run_on_render_executor`), `system` (health, status, logs,
  restart), `settings`, `display` (frame.png, next/pause/resume/show), `rotation`, `apps`, `photos`, `recipe`.
- `static/`: no-build UI. `app.js` hash router; `js/{now,photos,editor,rotation,apps,settings}.js` views;
  `ui.js`, `api.js`, `upload.js` helpers. Budget 300 KB (`tests/test_static.py`).
- Outside the package: `install.sh`, `deploy.ps1`, `deploy/{bootstrap.ps1, inkframe.service,
  post-receive-main, sudoers-inkframe}`, `scripts/` (dither builds, stats, blue-noise generator),
  `vendor/waveshare/` (reference only, never imported), `tests/`, `docs/`.

## Contracts that must not drift

- **Palette.** Index order `0 black, 1 white, 2 yellow, 3 red, 4 blue, 5 green` (`BLACK..GREEN`,
  `NAMES`). Panel codes `[0x0, 0x1, 0x2, 0x3, 0x5, 0x6]` (skips 0x4). The plan's table is wrong; never
  reintroduce it. Frames are landscape `(400, 600)` uint8 indices; the controller frame is portrait 400x600,
  120000 bytes, high nibble first; `pack_frame(frame, rotation)` rotation is 90 (default) or 270.
  Cached renders are 4-bit paletted PNGs whose pixel values are the frame.
- **Display protocol.** `kind`, `width`, `height`, `init()`, `show(frame)` (blocks ~19 s), `clear()`,
  `sleep()` (idempotent, never raises), `close()`, `awake`, `state` in `asleep|awake|refreshing`
  (lock-free). `show()` while asleep raises `DisplayNotReady`. Callers wrap every refresh in `try/finally: sleep()`.
- **App protocol.** Module `inkframe.apps.<id>` exports `APP` with `id, name, description, needs_dither,
  settings_schema (tuple[Field]), variants, render(ctx) -> RGB 600x400 | (400,600) index array`. Register the
  id in `APP_IDS`. `needs_dither=False` apps must emit only `NOMINAL` colours (else a loud `ValueError`).
  Network/data trouble must produce a card (cache or "offline"), `AppError` only for real failure.
  `RenderContext` fields: `settings, now, cache_dir, fonts, variant, seed, online, http_timeout,
  measured_palette, db, paths, app_settings, item_id`. Photos reads the global `Settings` from `app_settings`.
- **Recipe keys** (exactly these ten): `method, serpentine, strength, distance, brightness, contrast,
  saturation, gamma, sharpen, palette`. Ranges in `config.RECIPE_RANGES`; `distance` in `rgb|lab`; `palette`
  in `nominal|measured`. `settings.palette.use_measured` is the global switch: off, every photo dithers
  against `NOMINAL` whatever `recipe.palette` says (`pipeline.resolve_render_palette`).
  Render key = sha256[:16] of recipe + both palettes + `FORMAT_VERSION` + engine ABI/`dither.c`/blue-noise digest.
- **/api/status** (every key always present, unknown = null): `version`; `display{kind,width,height,awake}`;
  `panel{state,last_refresh_at,last_attempt_at,last_source{type,id,label[,variant]},last_error{at,kind,app_id,
  message}}`; `rotation{paused,quiet,quiet_until,current{app_id,label,photo_id,started_at,ends_at},next,next_at,
  keepalive_at,pending{kind,id,label,fires_at}}`; `settings{refresh_floor_seconds,keepalive_hours,quiet_hours,
  clear_before_refresh}`; `system{uptime_seconds,hostname,data_dir,disk_free_bytes,display_env,storage{masters_
  bytes,renders_bytes,cache_bytes,db_bytes}}`. `static/js/now.js` depends on it.
- **Errors.** Every non-2xx body is `{"error": "<message>"}` (422 names the field: `rotation[2].duration_minutes ...`).
  Photo ids are `uuid4().hex`; anything else is a 404. Times in the DB and API are ISO-8601 UTC with milliseconds.
- **DB state keys.** `scheduler` (JSON blob, `SchedulerState`), `last_refresh_at`, `last_source` (mirrors the
  UI/status read). Data dir files: `settings.json`, `inkframe.db`, `masters/`, `renders/`, `cache/<app_id>/`,
  `last_frame.png`, `last_frame.bin`.

## Invariants

- **Panel care is enforced in code** (`scheduler.py`, docs/SCHEDULER.md R1-R7): floor (default 180 s, >=30)
  from the end of the last attempt, failed attempts count; show-now is the only bypass and still waits
  `show_now_min_seconds` (30); keep-alive re-shows the last frame (<= `keepalive_hours`, default 20, max 23) even paused
  or quiet; quiet hours block rotation only; floor <= keep-alive x 3600; a failed render skips its slot before the panel is
  touched; the floor and keep-alive survive restarts and clock steps (wall + monotonic clocks).
- **Only the scheduler drives the panel.** Previews, uploads and APIs never call `init/show/clear`.
  Every refresh is `init -> [clear] -> show -> sleep` with `sleep()` in `finally`; nothing may leave the panel powered.
- **Host/Origin check** (`app._RequestGuardMiddleware`). No auth, but any method other than GET/HEAD/OPTIONS
  gets a 403 unless `Host` names the frame (`localhost`, `inkframe`, `*.local`, private/loopback/link-local IP,
  the hostname, or `INKFRAME_ALLOWED_HOSTS`, comma-separated) and `Origin`, if present, matches it (no
  cross-site POSTs, no DNS rebinding). TestClient counts as local. New routes are covered; never bypass it.
- **Request limits.** Bodies are capped at `app.MAX_BODY_BYTES` (1 MiB, 413) on every route but
  `POST /api/photos` (25 MiB, streamed); app settings text and list fields have length and count caps.
- **Nothing blocking on the event loop.** Decode/resize/adjust/dither/PNG-encode go through
  `api.run_on_render_executor` (`app.state.render_executor`, ONE thread, shared with the scheduler); plain `def`
  routes run in the threadpool. Never `time.sleep`, sync network or heavy numpy in an `async def` route.
- **Integer-only dither.** `dither.c` has no floating point in per-pixel paths (strength is converted once),
  so MSVC/x64 and gcc/aarch64 agree bit for bit (120 shared goldens). No `-ffast-math`/`/fp:fast`. No Python fallback.
- **LF endings** everywhere (`.gitattributes`; `*.ps1` are CRLF). Shell scripts run as `bash x.sh`, never `./x.sh`.
- **No new dependencies.** Pins in `requirements.txt` == `pyproject.toml`; every pin and its dependencies
  need wheels (cp313 manylinux aarch64 for compiled ones): `install.sh` installs with `--only-binary=:all:`,
  so a missing wheel fails the deploy. The Pi compiles only `dither.c`. numpy stays 2.2.x (last with 3.10).
- No secrets exist in the repo; keep it that way. Uploads: sniff magic bytes, 25 MiB, 60 MP, 150 MB ingest budget,
  names sanitised and kept in the DB only, files named by id, `Image.MAX_IMAGE_PIXELS` never changed.
- Failures are logged (with traceback) and degrade; the process must boot even with corrupt settings/state rows.

## Conventions

- Ruff: line length 100, rules E,F,W,I,B,UP, `from __future__ import annotations` required; run `ruff format`.
- Loggers are `logging.getLogger("inkframe.<package>.<module>")` (e.g. `inkframe.scheduler`,
  `inkframe.apps.weather`, `inkframe.api.photos`); never `print`. Settings/recipe validation raises `ValueError` naming
  the field (`panel.refresh_floor_seconds must be between 30 and 86400, got 10`); routes turn it into a 422.
- Write files atomically with `paths.atomic_write_bytes`. Time: use `scheduler.local_now()`/injected `now_fn`, never
  naive `datetime.now()` in decision code. Settings objects are immutable: `dataclasses.replace` or `Settings.merged`.
- Tests sit next to the feature in `tests/test_<module>.py`; a bug fix adds a regression test. Fake clocks
  (`conftest.FakeClock`, `Rig`) for anything time-based; no network ever (apps expose a `fetch_*` hook tests replace;
  conftest sets `INKFRAME_OFFLINE=1`, `INKFRAME_SCHEDULER=off`). Dither goldens are never edited by hand.
- Apps compose in palette colours via `apps.draw.Canvas`; type 14 px or larger; give each app 2-3 layout variants.
- One CHANGELOG.md entry per phase; conventional commit messages. Docs in docs/ are design notes: update them with the code.

## When changing X, also update Y

- `dither.c`: bump `ABI_VERSION` in both `dither.c` and `dither.py` if the calling convention changes; rebuild the DLL;
  `python -m tests.gen_dither_golden` only if output must change (review the JSON diff; run test_dither on the Pi too);
  docs/DITHER.md figures. The push triggers a full update on the Pi (`FULL_UPDATE_RE`). Render keys change by themselves
  (digest); bump `pipeline.FORMAT_VERSION` for Python-side output changes (fit, adjust, PNG layout, `dither.py`).
- Palette (indices, names, codes): `display/palette.py`, `apps/draw.py` constants, `test_palette.py`,
  `test_epd4in0e.py`, dither goldens, `dither.c` callers, the PNG colour table in `render/cache.py`, UI swatches (`settings.js`, `app.css`).
- A recipe key or range: `config.DEFAULT_RECIPE/RECIPE_RANGES/validate_recipe`, `pipeline.complete_recipe/recipe_key`,
  `adjust.apply`, `api/recipe.py` limits (automatic), `editor.js` sliders, `FORMAT_VERSION`, docs/PIPELINE.md, tests.
- `deploy/inkframe.service`: any change triggers a full update on the next push; keep `TimeoutStopSec` well above
  `__main__.GRACEFUL_SHUTDOWN_SECONDS` (30 s) + `app.SCHEDULER_STOP_TIMEOUT` (100 s). `deploy/sudoers-inkframe` must equal the copy embedded in `install.sh`
  (`sudoers_content`). The post-receive hook in `install.sh` is only rewritten by bootstrap or a full install.
- `requirements.txt`: mirror in `pyproject.toml`, confirm wheels for the pin and every new transitive dependency
  (cp313 aarch64; `--only-binary=:all:` fails the deploy otherwise), keep 3.10 support.
- An env var (`INKFRAME_*`): README env table, and the unit file if the Pi needs it.
- A settings key (`config.py`): `Settings.to_dict/from_dict/_pick` lists and `_TOP_KEYS`, `/api/status` `settings` subset
  if shown, `static/js/settings.js` / `rotation.js`, README table, `tests/test_config.py`. App settings keys are
  schema-driven (the form regenerates) but update the README app table and the app's tests.
- A new app: module with `APP`, id in `APP_IDS`, fonts via `FontLibrary`, tests with fixtures, README table, docs/previews.
- `/api/status`, scheduler snapshot or `SchedulerState` fields: `now.js`, `tests/test_api*.py`, docs/SCHEDULER.md tables.
- A static JS import or new file: `tests/test_static.py` checks imports resolve and the 300 KB budget; docs/UI.md.
- A DB schema change: append to `db.MIGRATIONS` (never edit an applied one) and add a migration test.
- Scheduler timing constants or rules: docs/SCHEDULER.md and the matching fake-clock test.

## Known traps

- Still unverified on the panel: the measured palette (`MEASURED_DEFAULT_RGB` is a guess until tuned from a photo of
  the test card), `clear_before_refresh`, and the 60 s busy timeout on a cold panel. docs/ROUGH_EDGES.md section B.
- `Scheduler.invalidate()` IS wired (fixed in Phase 8): a recipe change marks the panel stale and the next rotation
  change repaints the photo; nothing refreshes early. `palette.use_measured` IS a global switch (fixed in Phase 8):
  off forces `NOMINAL` for every photo. Older notes that say otherwise are stale.
- Both pinctrl lines in the unit start with `-`, so a missing or failing pinctrl is silent; check the journal.
- This frame's 270 orientation lives in its data dir (`settings.json`), not in the code: with a fresh data dir the
  panel hangs upside down until `display.rotation` is set to 270 again.
- Check docs/ROUGH_EDGES.md (section A) before relying on a behaviour it lists.
