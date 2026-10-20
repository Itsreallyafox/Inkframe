# Known rough edges

What is still rough in Inkframe as of 2026-10-20, after the Phase 8 review. Section A is open in
the code; section B needs the owner, the panel or a phone. One line per item, grouped by area, with
the file to look at. Items leave this list when they are fixed; `CHANGELOG.md` has the history.

## A. Open in the code

### Scheduler and state (`inkframe/scheduler.py`)

- Deleting or excluding the photo on the panel does not change the panel until its dwell ends (by design; `api/photos.py` `_delete_job`).
- Show-now requests live in memory only (`_pending`), so a restart drops a pending one.
- An NTP step followed by a restart within 300 s is never folded into the persisted anchors, so the floor or show-now minimum can be short by the step across that restart (`_observe_clock`; docs/SCHEDULER.md, Known limitations).
- `last_frame.bin` and `frame_rotation` are written in two steps, and `_persist_frame` returns None when the PNG write fails after the bin was written; either way a crash after a rotation change can make the next keep-alive draw the frame upside down.
- A render that kills the process (a segfault in `dither.c`, an OOM kill) is re-planned after every systemd restart: `_plan` prefers the due rotation item over the keep-alive re-show and there is no "render in progress" marker.
- While the render executor is held by a long job, each scheduler render waits out `RENDER_TIMEOUT_SECONDS` (180 s) in turn, so a due keep-alive can slip by several slots x 180 s before the 60 s backoff lets the re-show through (`_plan`, `_render`).
- `/api/status` lists the photos once or twice per poll to compute current and next (`snapshot` via `_rotation_plan`): fine for hundreds of photos, not thousands; each refresh also costs 3 to 4 small SQLite transactions (SD wear).
- A fractional `dwell_minutes` stored through `/api/settings` (2.5) comes back from `GET /api/rotation` as is and is refused by `PUT /api/rotation` as not whole, so GET then PUT is a 422 (`api/rotation.py`).
- `render_to_frame` returns the dither engine's output unchecked; the scheduler validates every frame, the app preview endpoint trusts the engine (`apps/base.py`, `api/apps.py` `render_preview_png`).
- On a DST night `quiet_window_active`'s `until` can be an hour early in real time; the scheduler wakes, sees the window still active and waits again (self-correcting).
- `show_now(photo_id=...)` reads the row on the API thread: a recipe that is not JSON gives a 422 with the decoder's message and a failing database a 500, neither a clean 404.

### Pipeline and uploads (`inkframe/render/`, `inkframe/api/photos.py`)

- Starlette spools an upload over 1 MB to `/tmp`, which is tmpfs (RAM) on Trixie, so two 25 MiB uploads can briefly hold 2 x 25 MiB twice (`MultiPartParser`; docs/PIPELINE.md "Not counted").
- A crash between writing `masters/<id>.jpg` and the DB insert leaves an orphan master, and nothing sweeps them at start-up (`render/masters.py` `ingest`, `api/photos.py` `_ingest_job`).
- Stale renders from an old `FORMAT_VERSION` or palette are only reclaimed by the per-photo prune to 40, never globally (`render/cache.py`).
- No coalescing on the one render thread: rapid recipe PUTs or render misses queue serially, and only the editor's debounce protects the Pi (`api/photos.py`).
- The HEIF decode path never runs `scan_metadata` or the EXIF/IFD walker, and whether pillow-heif materialises EXIF tags was never checked (`render/masters.py` `_decode_heif`).
- Also unchecked against `_jpeg_exif_block` and `_png_exif_block`: several APP1 Exif segments, MPO per-frame EXIF, and which of several `eXIf` chunks Pillow keeps (`render/masters.py`).
- nclx-only HEIF colour (no ICC profile) is not colour-managed (`render/masters.py` `_decode_heif`).
- The memory model was measured on the desktop only; the Pi's idle service RSS (82 to 86 MB plus 25 MB swapped) is above the 62 MB baseline the 150 MB budget is derived from, and real iPhone grid-tiled HEICs were never measured (docs/PIPELINE.md).
- 89 to 179 MP uploads (refused) print Pillow's `DecompressionBombWarning` to the journal; multi-page TIFFs with more than 32 IFDs are refused outright (`render/masters.py` `_MAX_IFDS`).
- The dither CLI's `_read_rgb` drops alpha instead of compositing on white (CLI only, not the pipeline; `render/dither.py`).
- The ordered methods' palette-tied spread varies a lot with palette edits and can reach 0 for a degenerate palette, where bayer and blue noise equal `none`; the UI never shows it (`render/dither.c` `ordered_spread`).
- CIE76 on this sparse palette puts some mid greys nearer the dull measured green than black or white, so the default recipe flattens light and dark greys; CIEDE2000 would be the engine fix, palette tuning the practical one (docs/DITHER.md).

### Apps (`inkframe/apps/`)

- Weather geocoding misses are not cached, so a misspelled place is queried again on every render (`weather.py` `geocode`).
- Weather's imperial units also switch to a 12-hour clock (no separate setting), there is no wind direction, and notice cards use the frame's clock while data cards use the location's (`weather.py`).
- The weather card shows no Open-Meteo credit (data is CC BY 4.0); the ZenQuotes card does carry its attribution (`weather.py`).
- Headlines has no raqm, so no OpenType features or complex-script shaping: CJK or Arabic titles print as missing glyphs and RTL is not handled (`headlines_type.py`, bundled fonts).
- The headlines video/audio/quiz/live-blog filters match English only (`headlines_text.py`); an abandoned fetch thread ends with its own httpx timeout rather than being killed (`headlines_data.py`).
- Headlines `count` is a maximum: v2 always shows 3 stories, v0 a lead plus up to 4, v1 up to 6 (`headlines_layouts.py`).
- Quotes' `recent.json` is updated by every render including UI previews, so previews use up the "not recently shown" pool; ZenQuotes quotes are reused for 90 s to keep preview and panel in step (`quotes.py`, `quotes_data.py`).
- ZenQuotes text with digits is set in Playfair's old-style figures, and the attribution is the text "Quotes by ZenQuotes.io" because a card cannot carry their link (`quotes.py`).
- The 311 bundled quotes were attributed from knowledge, not checked one by one; a few rest on second-hand sources the entry names (`apps/data/quotes.json`).
- The first render in a process is slow (quotes 0.5 to 0.65 s on the desktop, so seconds on the Pi), and no Phase 5 app was timed on the Pi (`quotes.py`, `patterns.py`).
- The Patterns, Weather, Headlines and Quotes renders have no golden-image tests, since numpy trig, SIMD and FreeType differ by an ulp or a pixel between platforms (`tests/test_app_*.py`).

### UI (`inkframe/static/`)

- `panel.show_now_min_seconds` has no UI field, the Rotation view can add an app slot but not remove one (switch it off instead), and there is no in-app light/dark switch (`js/rotation.js`, `app.css`).
- No automated browser tests: all UI QA was manual in Chromium device emulation at 375/390/768/1280 px (`tests/test_static.py` checks only imports and the 300 KB budget).
- Screenshots in `docs/ui/` have mixed resolutions, nothing regenerates them or `docs/previews/`, the headlines previews are live data of 19 Oct 2026, and the desktop and storage Settings shots were removed because they showed a Windows home path.
- `/docs` (FastAPI's Swagger page) loads its assets from a CDN, so it only renders with internet (`app.py`).

### Deploy and scripts (`deploy.ps1`, `install.sh`, `deploy/`)

- `deploy.ps1 -Branch` pushes without force, so a diverged branch is rejected (push `+branch:main` by hand), and snapshot commit objects pile up in the local `.git` (`deploy.ps1`).
- The post-receive hook and the sudoers drop-in reach the Pi only through `--bootstrap` or a full install, both of which need general passwordless sudo (`install.sh` `hook_script`).
- Under concurrent pushes the hook checks out the current `main` rather than the pushed sha, so `last-deploy` can briefly name an older one; re-running a failed deploy without a commit is manual (`install.sh`, `deploy/post-receive-main`).
- `journalctl -u inkframe` without sudo may show nothing; the Settings log tail is an in-memory ring of 500 lines that a restart clears, and the Pi has no persistent journal (`inkframe/log.py`).
- pip prints a cosmetic "dependency resolver" ERROR about Debian's `types-seaborn` on every full update (docs/DEPLOY.md, What the hook does).
- The unit has no `ProtectSystem=` style hardening, on purpose: it would hide `/dev/spidev0.0` (`deploy/inkframe.service`).
- No env var makes the mock simulate the 19 s refresh; `MockDisplay(refresh_seconds=)` exists but the factory passes 0 (`display/factory.py`).

### Tests (`tests/`)

- Python 3.13 has never run on the desktop, and the Pi ran the test suite on 3.13.5 for Phases 1 to 3 only; the code itself has run on the Pi since Phase 7.
- On the Pi, `test_masters_memory.py` and `test_api_photos.py::test_desktop_timings` need the service stopped, the full suite once OOM-killed pytest, and the chunked test-image generator (commit `f98bd2c`) was not re-run there.
- `pyproject.toml` filters Starlette's deprecation of `httpx` for `TestClient`; moving off it is undecided.
- `tests/fixtures/` holds trimmed real BBC, NYT and Guardian feeds and Open-Meteo responses (third-party text in the repo).

## B. Needs the owner, the panel or a phone

- Tune the six measured colours from a photo of the test card (`display/palette.py` `MEASURED_DEFAULT_RGB` is a guess), then re-judge the default recipe's greys and the ordered-spread effect.
- Decide `display.clear_before_refresh` (off by default; doubles the refresh time) and whether the 180 s floor, 30 s show-now minimum, 20 h keep-alive and 23:00 to 07:00 quiet hours suit the owner.
- `busy_timeout_s` is 60 s; the first real refresh took 19.1 s, but a cold-panel refresh has not been timed, and one slower than the timeout is cut off by design (`display/epd4in0e.py`).
- CE0 is left to spidev at exit, unlike the vendor C builds that drive CS low (possible minor back-powering through CS; low priority; `display/epd4in0e.py`).
- No panel temperature is available from the vendor driver, so none is logged, and `state` reads "asleep" during the ~0.5 s `init()` (`display/epd4in0e.py`).
- Look at the real panel from 2 m: patterns' 2 px keylines, flow contours and 14 px mono caption; weather's dotted texture and hairline fog; headline and quote type sizes; the test card's 1 px and 2 px line sets.
- Test the UI on an iPhone (Safari): `createImageBitmap` orientation, native HEIC decode and the HEIC-in-Chrome fallback, real-touch Rotation drag, long-press on method tiles, input zoom, safe-area insets, the sticky editor stage, two-file upload decode memory.
- Verify on the Pi: the Restart button's `Restart=always` recovery, `systemctl stop` during a refresh, a `kill -9` during a refresh (the `ExecStopPost` pin safety net), mDNS from the phone.
- Finish the overnight soak: keep-alive through quiet hours, no leaks.
- Choose a licence for Inkframe, and decide whether the weather card should credit Open-Meteo.
