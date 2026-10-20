# Web UI

The frame's web page: six views, vanilla ES modules, one stylesheet, no
build step and nothing from a CDN (the frame may be offline). FastAPI serves
`inkframe/static` at `/static` (revalidated on every load, so a deploy shows
up at once) and `index.html` at `/`. The bundled fonts are served at `/fonts`
with a one-year cache; the UI loads exactly one of them,
`PlayfairDisplay-Regular.ttf`, for titles. Everything else uses the system
sans (`-apple-system, Inter, Segoe UI, Roboto`) and `ui-monospace`.

Budget: HTML + CSS + JS stay under 300 KB uncompressed (about 215 KB today;
`tests/test_static.py` enforces it).

Screenshots of the views at phone and desktop width are in `docs/ui/` (the Settings page only at phone width).

## Files

```
inkframe/static/
  index.html        shell: top bar (phones), nav, <main id="view">, toasts, confirm dialog,
                    inline SVG favicon (six squares)
  app.css           all styles: tokens, light + dark (prefers-color-scheme), components, views
  app.js            hash router, nav icons, connection badge
  js/ui.js          h() element builder, icons, toast(), confirmDialog(), debounce(),
                    formatting (clock, countdown, bytes), segmented control, switch
  js/api.js         api() fetch helper, fetchImage(), connection state, server clock offset,
                    cached /api/recipe/default, queuedMessage()
  js/upload.js      browser-side downscale + XHR upload queue (parallelism 2)
  js/now.js         Now
  js/photos.js      Photos (grid, actions, uploader)
  js/editor.js      Photo editor
  js/rotation.js    Rotation (slots, drag reorder, quiet hours, panel care)
  js/apps.js        Apps (generated settings forms, previews)
  js/settings.js    Settings (palette, display, storage, log, restart)
```

## Routing

Hash routes, so the server only ever serves `/`:

| Hash | View | Tab |
|---|---|---|
| `#/now` (default) | `nowView` | Now |
| `#/photos` | `photosView` | Photos |
| `#/photos/<32 hex id>` | `editorView` | Photos |
| `#/rotation` | `rotationView` | Rotation |
| `#/apps` | `appsView` | Apps |
| `#/settings` | `settingsView` | Settings |

A view is a function `view(container, ...routeParams)` that fills the
container and returns `{destroy, isDirty}`. The router calls `destroy()`
before mounting the next view (stop timers, pollers, document listeners) and
asks `isDirty()` before leaving: with unsaved edits it puts the old hash
back and asks "Discard your changes?" first. `beforeunload` uses the same
hook.

## Views and the endpoints they use

| View | Reads | Writes |
|---|---|---|
| Now | `GET /api/status` every 5 s while the tab is visible (paused when hidden), `GET /api/display/frame.png?v=<last_refresh_at>` | `POST /api/display/next`, `/pause`, `/resume` |
| Photos | `GET /api/photos`, each photo's `render_url`, `GET /api/recipe/default` (method names) | `POST /api/photos` (XHR, multipart), `POST /api/display/show {photo_id}`, `PUT /api/photos/{id}/flags`, `DELETE /api/photos/{id}` |
| Editor | `GET /api/photos/{id}`, `GET /api/recipe/default` (methods, limits), `POST /api/photos/{id}/previews {knobs, methods}`, `GET /api/photos` (how many photos follow the default) | `PUT /api/photos/{id}/recipe` (knobs, or `{"reset": true}`), `PUT /api/recipe/default`, `POST /api/display/show {photo_id}` |
| Rotation | `GET /api/rotation`, `GET /api/settings`, `GET /api/apps` (names for "add") | `PUT /api/settings {quiet_hours, panel, display}`, then `PUT /api/rotation {slots}` |
| Apps | `GET /api/apps`, `GET /api/apps/{id}/preview.png?variant=&seed=` | `PUT /api/apps/{id}/settings`, `POST /api/display/show {app_id}` |
| Settings | `GET /api/settings`, `GET /api/status` (display, storage, uptime), `GET /api/system/logs?n=200`, `GET /api/apps/patterns/preview.png?variant=2` | `PUT /api/settings {palette}` / `{display: {rotation}}`, `POST /api/display/show {"app_id": "patterns", "variant": 2}`, `POST /api/system/restart` |

Backend pieces added for the UI: `GET /api/display/frame.png` (the panel's
`last_frame.png`, ETag from mtime and size), the optional `variant` on
`POST /api/display/show` (passed through the scheduler into the render
context, so the test card shows once without touching the pattern
settings; a forced variant is also recorded in `panel.last_source.variant`,
which is how Now says "Palette test card"), `POST /api/system/restart`,
`system.storage` in `/api/status` (walked in the threadpool, reused for
30 s), the `/fonts` mount, and the optional `"methods": [...]` filter on
`POST /api/photos/{id}/previews` (renders only those methods; the editor's
live preview asks for one).

The UI holds no state the server does not: forms keep a draft until Save,
and every answer replaces the draft with what the server stored.

## Errors and feedback

`api(path, {method, body, quiet})` parses JSON and turns any non-2xx answer
into an `ApiError` carrying the server's `{"error"}` text. Without `quiet`
the message is also shown as a toast; views that show errors inline
(Rotation's 422s, the Apps forms) pass `quiet: true` and place the message
under the field it names (`rotation[2].duration_minutes ...` marks that
slot's input). A network failure flips the connection badge to Offline.

There is no login, so the server keeps other web pages out instead (see the
`inkframe/app.py` docstring): every request but GET/HEAD/OPTIONS must name
the frame in `Host` (`localhost`, `inkframe`, `*.local`, a private or
loopback IP, the Pi's hostname, or a name in `INKFRAME_ALLOWED_HOSTS`) and,
when the browser sends `Origin`, come from that same host; anything else is
`403 {"error": "request refused: ..."}`. Opening the UI through another name
(a reverse proxy, a DNS name) needs that name in `INKFRAME_ALLOWED_HOSTS`.
Request bodies are capped at 1 MiB (`413`, before the body is read); uploads
keep their own 25 MB limit.

Toasts: `toast(message, "ok" | "warn" | "error" | "info")`, bottom of the
screen above the tab bar (bottom right on desktop). Deletes and the restart
go through `confirmDialog()` (a native `<dialog>`; Cancel has the focus).

## The upload pipeline (js/upload.js)

1. Decode with `createImageBitmap(file, {imageOrientation: "from-image"})`,
   else an `<img>` (which applies EXIF orientation by default).
2. Draw onto a canvas with the long edge at 1800 px (never upscaled), on a
   white background (transparent PNGs land on the panel's white), and export
   JPEG quality 0.92.
3. If decoding fails (HEIC in Chrome or Firefox), send the original bytes;
   the server converts them.
4. `XMLHttpRequest` with `upload.onprogress` for the bar; when the body has
   gone the row says "Rendering…" until the 201 arrives with the photo, which
   is put at the top of the grid. 413/415/422/408 messages stay on the row.

`UploadQueue` runs two at a time (the server holds two upload slots), but
decodes one file at a time: a full-size bitmap of a 24 MP phone photo is
about 96 MB, and two of them at once is more than an iPhone tab may hold.
Once a file's bytes have gone ("Rendering…") the row offers no Cancel: the
frame finishes the photo either way. Uploads keep running if you leave the
page.

## The editor's preview loop (js/editor.js)

`POST /api/photos/{id}/previews` renders one set of knobs with the methods
it is asked for (`"methods": [...]`, or all twelve) and answers with a URL
per method; repeating it is cheap because renders are cached by key. The
editor keeps every answer per knob signature and has one request in flight;
`pump()` picks the next one:

1. the selected method with the current knobs, alone (a slider move costs
   one dither on the Pi, not twelve);
2. once the knobs have settled for 1.5 s, or on Refresh (R), the other
   methods, four per request, so a new slider move never waits behind a
   long twelve-method render.

Knob changes are debounced 300 ms; an answer for knobs that are no longer
current is kept (going back to them is instant) but not shown. Until the
strip has caught up, its tiles are dimmed and the Methods heading says
"updating". The caption under the stage times the stage's own render.

Layout: the stage is sticky. On phones and tablets `.editor-main` is
`display: contents`, so the stage stays at the top of the screen while the
strip and then the sliders scroll beneath it; the bezel is capped by the
viewport height so a landscape phone keeps room for the controls. From
1100 px the stage sticks within the left column (the full 620 px bezel, the
render at 1:1, from 860 px of height) and the adjustments panel is sticky
on the right and scrolls on its own. On phones a save bar rises with
unsaved edits, hidden while the inline Save is on screen.

Tiles show a Detail by default: the same 72 x 48 panel pixels of every
render at about twice their size, unsmoothed, so the grain of each method
can be compared (Whole shows the full picture, D toggles). Click or tap the
stage to choose the detail; it is outlined there. The method's description
sits under the strip with three lines reserved, so it never moves the strip.

"Make this the default…" names what it changes: the confirm lists the
method and knob changes and how many photos follow the default (they all
re-render with it), and the note under the link says the same.

The stage is the real 600 x 400 render with `image-rendering: pixelated`;
zoom is Fit, 1:1 or 2x inside a 3:2 window centred on the detail, panned by
dragging (mouse) or swiping (touch), with no scrollbars inside the bezel.
Keys: arrows pick a method, Z cycles zoom, D detail/whole, R refreshes the
strip, Ctrl/Cmd+S saves, Esc goes back.

## Rotation drag and drop

Pointer events on the handle (`touch-action: none`, pointer capture). The
dragged row follows the pointer with a transform; when its centre passes a
neighbour's centre the two swap in the DOM and the transform is corrected
by the neighbour's height. Within 64 px of the top or bottom of the visible
band (between the nav, the tab bar and a save bar) the page scrolls by
itself, faster nearer the edge, and the scrolled distance is added to the
row's offset so it stays under the finger. The new order is read back from
the DOM on release. The handle also takes ArrowUp/ArrowDown, and each row
has up/down buttons.

The cycle bar above the slots shows, per segment, as much as fits without
cutting a word: name and minutes, the name alone, the slot's number from
the list ("02"), or nothing.

## Design

- Paper `#f4f3ee`, ink `#18181a`, hairline rules on an 8 px grid; no
  gradients, one shadow (a lifted row or floating save bar).
- The six panel colours only as meaning: yellow pending/warning (portrait
  badge, queued show-now, quiet hours, "rendering"), red destructive/error,
  blue primary actions, links and focus, green ok/online. The six-swatch
  strip is the signature (brand mark, favicon, empty states, the palette
  editor, and the "refreshing" chaser along the bottom of the frame).
- The frame's image sits in a dark bezel, pixel-perfect (600 px at 1:1 on
  desktop, on whole pixels: the view is centred with `round()` and the
  titles above it have whole-pixel line heights; scaled with `pixelated` on
  phones), with a monospace caption.
- Times use the device's clock style everywhere (quiet hours included), and
  numbers never break from their units (non-breaking spaces in the
  formatters in `ui.js`).
- Phones: bottom tab bar (64 px + safe area), 44 px touch targets, 16 px
  inputs (iOS does not zoom). From 768 px a top nav; from 1100/1200 px the
  Now, Editor, Rotation and Settings pages go two-column.
- Dark mode follows `prefers-color-scheme` (tokens only; the panel colours
  and the frame stay the same).

## Adding a view

1. Write `inkframe/static/js/<name>.js` exporting `function <name>View(root)`
   that appends to `root` and returns `{destroy, isDirty}`. Use `h()` for
   DOM (it sets text, never HTML), `api()` for requests and `pageHead()` /
   `eyebrow()` for the page furniture.
2. Import it in `app.js` and add a route to `ROUTES`.
3. For a new tab, add an `<li>` to the nav list in `index.html` (an icon name
   from `ui.js`'s `ICONS`). The bottom bar has room for five tabs; a sixth
   view belongs under an existing tab, as the editor does.
4. Run `pytest tests/test_static.py`: it checks that every import resolves
   to an exported name and that the size budget holds.
