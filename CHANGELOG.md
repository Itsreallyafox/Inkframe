# Changelog

All notable changes to Inkframe. One entry per build phase; dates are UTC.

## 0.1.0 — Phase 4: web UI (2026-10-20)

- Mobile-first no-build UI (184 KB of HTML/CSS/JS, one font file): Now
  (the last frame pixel-perfect in a bezel, what is showing and next,
  pause/resume/show-next, panel status), Photos (grid of dithered renders,
  drop zone with browser-side downscale and per-file progress), Editor
  (sticky stage, Detail-mode method strip, debounced single-method live
  preview, zoom, save / make default / reset / show), Rotation (drag reorder
  with auto-scroll, slot settings, cycle timeline, quiet hours, floor,
  keep-alive), Apps (forms from each app's schema, previews), Settings
  (palette editor, test card, rotation, storage, logs, restart).
- Backend: POST /api/system/restart, storage usage in /api/status,
  GET /api/display/frame.png, optional "variant" on POST /api/display/show,
  optional "methods" filter on POST /api/photos/{id}/previews.
- Independent UX review in the browser at 375/390/768/1280 px; all critical
  and design findings fixed; screenshots in docs/ui/.

## 0.1.0 — Phase 3: photo pipeline and API (2026-10-19)

- `inkframe/render/masters.py`: upload ingest. Formats are sniffed from
  magic bytes (JPEG, PNG, WEBP, HEIC/HEIF, TIFF, BMP, GIF) and opened with
  that one Pillow plugin only. Limits: 25 MB, 60 MP, and a 150 MB ingest
  memory budget (a per-stage estimate from the headers alone, checked
  before any pixel is decoded, plus the upload's bytes; calibrated against
  measured peaks for every format, including WebP's decoder canvases,
  progressive JPEG coefficient buffers and TIFF strips). JPEGs decode
  drafted at 1/2 to 1/8 scale; HEIC goes through libheif's buffer,
  box-reduced into Pillow strip by strip (a 24 MP iPhone photo peaks at
  +120 MB instead of +181); other formats are converted to 8 bits,
  composited on white and box-reduced strip by strip. EXIF orientation
  (once, TIFF included), transparency on white (`tRNS` colour keys too),
  16-bit scaling and ICC to sRGB are handled. Masters are stored as
  `masters/<id>.jpg` (at most 1800 px, q92, no metadata, atomic write, never
  replaced); ids are validated before they become paths; names are
  sanitised to 120 characters and kept in the DB only.
- `inkframe/render/pipeline.py`: fit (Lanczos letterbox on white, portrait
  never rotated), then adjust, then dither, last and 1:1. Only the photo's
  box is adjusted and dithered, so the bars are exact palette white whatever
  the recipe. Recipe validation includes the method registry. Effective
  recipe resolution (an override's unusable knob falls back alone),
  palette resolution, content-addressed render keys (sha256 over recipe,
  both palettes, `FORMAT_VERSION`, and the engine's ABI plus a digest of
  `dither.c` and the blue-noise texture), and a 4-entry fit LRU.
- `inkframe/render/adjust.py`: brightness, contrast, saturation (Pillow
  `ImageEnhance`), a numpy gamma LUT and unsharp mask, in a fixed order;
  neutral values are pixel-identical.
- `inkframe/render/cache.py`: `renders/<id>/<key>.png` as 4-bit paletted
  PNGs whose pixels are the frame's indices and whose colour table is the
  measured palette. Atomic writes, corrupt files treated as misses, prune to
  40 renders per photo, `render_or_get` and `render_methods` (adjust once,
  dither 12 times).
- `/api/photos`: list, multipart upload (413 by `Content-Length` and by
  streamed size, 415, 422; at most two uploads hold their bytes at once; a
  stored photo is a 201 even if its first render fails), read, render
  (immutable caching, cached renders served without the executor, the
  current key rendered on demand), 12-method previews, recipe set/reset (a
  body without fields changes nothing; out-of-range numbers, however large,
  are 422s), exclude flag, delete. `/api/recipe/default`: GET/PUT with the
  method registry and knob limits for the editor. All decode, resize and
  dither work runs on the shared one-thread render executor.
- `inkframe/apps/photos.py`: the real Photos app. It serves the cached frame
  for the scheduler's photo id (rendering on a miss), shows a "No photos
  yet" card when there is nothing to show, and raises `AppError` when a
  master is missing or the settings snapshot is not wired in.
- Ingest hardening: progressive MPOs (JPEGs with an MPF segment, as phones
  write them) count their coefficient buffer; a scan of the raw bytes,
  before Pillow parses them, refuses header floods its pure-Python parsers
  would spend minutes and hundreds of MB on (JPEG markers, PNG chunks, GIF
  blocks and quadratic GIF comment joining) and counts the PNG text and
  JPEG APP data Pillow keeps; RLE BMPs over 4 MP are refused (Pillow's
  Python decoder); 0..1 float TIFFs are scaled, not clipped to black;
  `Image.MAX_IMAGE_PIXELS` is no longer changed process-wide. Uploads whose
  body stalls for 30 s or takes over 300 s get a 408 and free their slot.
  A stored default recipe with an unknown method keeps its other knobs; a
  corrupt cached preview is re-rendered instead of being listed with a
  dead URL; a huge integer in `PUT /api/settings` (or `settings.json`) is
  a 422 (or a moved-aside file), not a 500.
- `docs/PIPELINE.md`, with the measured memory table; 277 new tests
  (843 in all), including a golden-by-equality end-to-end test
  (upload, previews, recipe, render bytes pack to 120000) and
  `tests/test_masters_memory.py`, which measures real ingest peaks in a
  fresh interpreter against the budget and the estimates.
- Hardening after review: an EXIF/TIFF IFD walker charges what Pillow would
  materialise from embedded metadata (every APP1 Exif segment, every PNG
  eXIf chunk, the WEBP EXIF chunk, a TIFF's own IFDs) against the decode
  budget before `Image.open`, with caps of 2000 entries per IFD and 32 IFDs;
  crafted 100 KB files that previously allocated 350+ MB are refused with 422.
- Pi pass: 2400x1600 upload 1.7 s, 12-method preview set 2.2 s (cached 0.1 s),
  service RSS peak 115 MB, scheduler shows the uploaded photo.

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
