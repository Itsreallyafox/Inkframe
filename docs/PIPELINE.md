# Photo pipeline: design notes

How an uploaded photo becomes the 600 x 400 six-colour frame the panel shows,
and how renders are cached. The code is `inkframe/render/` (`masters.py`,
`pipeline.py`, `adjust.py`, `cache.py`, plus the Phase 2 `dither.py`), the
HTTP side is `inkframe/api/photos.py` and `inkframe/api/recipe.py`, and the
scheduler's view is `inkframe/apps/photos.py`.

```
upload bytes ─► ingest ─► master  <data>/masters/<id>.jpg   (≤ 1800 px, JPEG q92, kept)
                               │
                               ▼
                  fit    600 x 400 RGB, Lanczos, white bars        (LRU, 4 entries)
                               │
                               ▼
                  adjust brightness → contrast → saturation → gamma → sharpen
                               │   (inside the photo's box only)
                               ▼
                  dither 1:1 at panel resolution, recipe's method and palette
                               │
                               ▼
                  frame  (400, 600) uint8 palette indices
                               │
                               ▼
                  cache  <data>/renders/<id>/<key>.png   (4-bit paletted PNG)
```

The order is the plan's (section 5) and it matters: **dithering is always the
last step, at exactly 1:1**. A dither pattern is tuned to the panel's pixel
grid. Scaling a dithered image destroys the pattern and creates moiré, so
nothing is ever resized after the dither, and previews are full-size renders
that the browser scales with `image-rendering: pixelated`.

## Stages

### 1. Ingest (`masters.ingest`)

Runs on the render executor. The upload's name and `Content-Type` are never
trusted.

1. **Size**: more than 25 MB is refused with `UploadTooLarge` (413). The API
   checks `Content-Length` before reading anything and counts the streamed
   body as it arrives, so a chunked or lying client is cut off at the limit
   too, even when the excess is after the multipart's closing boundary.
2. **Sniff** the magic bytes: JPEG `ff d8 ff`, PNG, WEBP (`RIFF....WEBP`),
   HEIC/HEIF (an ISO-BMFF `ftyp` box whose major or compatible brands include
   `heic heix hevc hevx heim heis hevm hevs`, or `mif1`/`msf1` without
   `avif`), TIFF (`II*\0`, `MM\0*`), BMP (`BM` plus a valid DIB header size)
   and GIF. Anything else, AVIF included, is `UnsupportedImageType` (415).
   Pillow then opens the file **with that one plugin only**
   (`Image.open(..., formats=[...])`), so a file cannot be smuggled through a
   different decoder.
3. **Bombs and memory**, all before any pixel is decoded and all
   `UndecodableImage` (422), as are corrupt and truncated files:
   - **Header structure** (`masters.scan_metadata`, on the raw bytes, before
     Pillow parses anything). Pillow parses JPEG markers, PNG chunks and GIF
     blocks in Python, one at a time, inside `Image.open`, and keeps an
     object for many of them: a 25 MB JPEG of empty APP1 segments would take
     seconds and hold about 750 MB (120 bytes per 4 bytes of file), and GIF
     comments are joined one 255-byte block at a time, quadratically (a
     4 MB comment takes 5 s on the desktop). The scan walks the structure
     the way Pillow does and refuses a file that needs more than 50 000
     steps (markers, padding and junk bytes, chunks, blocks; real files take
     a few hundred) or more than 256 MB of comment copying. It also counts
     what Pillow keeps: text, ICC and EXIF chunks (zTXt inflated to its 1 MB
     limit, UTF-8 iTXt at up to 4 bytes a character), private PNG chunks,
     and JPEG APP and COM payloads. Pillow already decodes a PNG's text
     before its pixels while it opens it, so that metadata alone must fit
     the budget before `Image.open` runs.
   - **Pixels**: `Image.open` parses the headers only, and anything over
     60 MP is refused straight after it. `Image.MAX_IMAGE_PIXELS` is not
     changed: it is process-wide, so another thread opening an image during
     an ingest would see a different value. Pillow's own limit (twice its
     default, about 179 MP) still refuses a larger image inside the open.
   - **Slow decoders**: an RLE-compressed BMP goes through Pillow's
     pure-Python decoder, whose end-of-line padding costs a loop step per
     pixel however small the file (about 100 ns a pixel on the desktop,
     several times that on the Pi), so RLE BMPs over 4 MP are refused rather
     than hold the one render thread for minutes.
   - **Memory**: `decode_cost` estimates the ingest's peak memory, which
     together with the upload's own bytes must fit a 150 MB budget (see
     [Memory](#memory)). The estimate uses only the raw bytes and what
     `Image.open` parsed from the headers. (Reading a PNG's EXIF decodes the
     whole image when its eXIf chunk follows the pixels, so the orientation
     is read only after the check.)
4. **Decode small**:
   - **JPEG**: `Image.draft` asks libjpeg for a 1/2, 1/4 or 1/8 scale decode
     that is still at least the master's size. A 48 MP phone JPEG never
     exists at full size in memory. The plan suggested twice the master size;
     that leaves a 24 MP (5712 px) JPEG undrafted at 98 MB, while libjpeg's
     scaled IDCT is already an area filter and the master is reduced another
     3x at fit time. A progressive (or non-interleaved, lossless or
     hierarchical) JPEG still makes libjpeg hold every DCT coefficient at
     full size, 2 bytes per sample, whatever the draft; the estimate counts
     that buffer (`decoder_overhead`). That includes MPOs: Pillow opens a
     JPEG whose MPF segment lists two or more images (phone photos with a
     preview or gain map) as an `MpoImageFile` of format `"MPO"`, whose
     primary image is drafted and decoded by the same libjpeg path.
   - **HEIC**: pillow-heif's own API, not its Pillow plugin. libheif decodes
     into its buffer, and `reduce_rows` hands Pillow box-reduced strips, so
     Pillow never holds a full-size copy (it stores RGB at 4 bytes per
     pixel). libheif applies the HEIF rotation and mirror boxes itself, and
     the EXIF orientation is then ignored, as the HEIF spec requires.
   - **Everything else**: Pillow decodes it, then `masters.normalise` brings
     it to 8 bits, composites any transparency on white and box-reduces it
     (`Image.reduce`) to no less than the master size, the same stopping
     point as the JPEG draft. A mode that needs converting is processed in
     horizontal strips of about 0.5 MP, each converted, composited and
     reduced before the next, so no second full-size image ever exists (a
     24 MP palette PNG would otherwise be expanded to a 96 MB RGBA copy, and
     `reduce` would premultiply that into another 96 MB). Compositing on
     white before the box average gives the same result as averaging
     premultiplied alpha, so edges between transparent and opaque pixels
     come out right (tested on an edge that splits a reduction block).
5. **Colour**: transparency is composited on white: RGBA, LA, palette images
   with `transparency`, and `tRNS` colour keys on RGB, grey and 16-bit grey
   PNGs. 16-bit greys are scaled to 8 bits rather than clipped, and float
   (`F`) TIFFs whose samples all lie in 0..1 are scaled by 255 (any other
   float range is taken as 0..255 and clipped). An embedded
   ICC profile is converted to sRGB with littlecms when it can be used, which
   matters for Display P3 iPhone photos; a profile it cannot use is ignored.
6. **Resize**: Lanczos, so the long edge is at most 1800 px (3x the panel).
   Smaller images are never upscaled here.
7. **Orientation**: the EXIF/XMP orientation is read from the decoded file
   and applied to the *master-size* image, after the resize (the long edge
   is the same either way), using `ImageOps.exif_transpose`'s table without
   its full-size copy. Pillow's TIFF plugin already turns a TIFF while
   loading it and drops the tag, so reading after the load also keeps a TIFF
   from being turned twice. Portrait photos stay portrait: they are
   letterboxed later, never rotated. The UI shows a badge from `portrait`.
   All eight orientations are tested for PNG, JPEG, WebP and TIFF.
8. **Save**: baseline JPEG q92 with optimised Huffman tables and no EXIF, ICC
   or XMP, written atomically (temp file, fsync, rename) to
   `masters/<photo_id>.jpg`. The original upload is discarded. A master is
   never replaced: `ingest` refuses an id whose master exists.

The sanitised original name, at most 120 characters, goes only to the
database. The sanitiser keeps the last path component, turns tabs and
newlines into spaces, drops control and invisible format characters such as
NUL, ESC and RTL overrides, and collapses whitespace. Files on disk are named
by the photo id (`uuid4().hex`); `masters.master_path`, `delete_master` and
`ingest(photo_id=...)` refuse anything else, and `pipeline.master_file` only
follows a stored `master_path` that stays inside `<data>/masters`, so a
database value never makes the pipeline read or delete another file.

### 2. Fit (`pipeline.fit_image`)

The photo is scaled to fit inside 600 x 400, keeping its aspect ratio
(Lanczos, up or down), and centred on white `(255, 255, 255)`. The result is
a `Fit`: the 600 x 400 canvas plus the `box` the photo occupies.

| Source | Box | Bars |
|---|---|---|
| 3:2, e.g. 1800 x 1200 | `(0, 0, 600, 400)` | none: fills exactly |
| 4:3, e.g. 1600 x 1200 | `(33, 0, 566, 400)` | left and right |
| portrait 2:3, e.g. 800 x 1200 | `(166, 0, 433, 400)` | left and right |
| 16:9, e.g. 1920 x 1080 | `(0, 31, 600, 369)` | top and bottom |

4:3 is *narrower* than the panel's 3:2, so it gets side bars, not top and
bottom ones. Only images wider than 3:2 get top and bottom bars.

### 3. Adjust (`adjust.apply`)

Applied to the pixels **inside the box only**, in a fixed order:

1. brightness: `ImageEnhance.Brightness`
2. contrast: `ImageEnhance.Contrast`, around the photo's own mean, not one
   skewed by the white bars
3. saturation: `ImageEnhance.Color`
4. gamma: a 256-entry numpy LUT, `255 * (v / 255) ** (1 / gamma)`, so
   gamma > 1 lifts the mid-tones
5. sharpen: `ImageFilter.UnsharpMask(radius=1.5, percent=round(sharpen * 100), threshold=2)`

A neutral value skips its stage, so a neutral recipe is a pixel-identical
copy (tested).

### 4. Dither (`pipeline.render_frame`)

The adjusted box is dithered with the recipe's method, `serpentine`,
`strength` and `distance`. The palette is the **measured** panel colours from
settings when `recipe.palette == "measured"`, otherwise the nominal
primaries. The bars are set to palette white (index 1) exactly.

Dithering only the box is deliberate:

- the bars stay clean whatever the adjustments (brightness 0.5 would
  otherwise turn them into grey speckle);
- no error diffuses between the bars and the photo's edges. Pure white is
  brighter than the measured white, so a white bar carries a steady positive
  error that would otherwise bleed a light fringe into the photo.

Output: a `(400, 600)` uint8 frame of palette indices, which
`display.palette.pack_frame` packs into the panel's 120 000 bytes.

## Recipes

A recipe is the complete dict `config.DEFAULT_RECIPE` describes: `method,
serpentine, strength, distance, brightness, contrast, saturation, gamma,
sharpen, palette`. `pipeline.complete_recipe` validates one with
`config.validate_recipe` (ranges, types, unknown keys) and also checks the
method against the dither registry. Every error is a `RecipeError` naming
the field, which the API returns as a 422; that includes a number too large
for a float (a JSON integer of 400 digits), which `float()` would otherwise
turn into an `OverflowError` and a 500.

- **Default recipe** (`settings.default_recipe`, plan section 13: atkinson,
  serpentine, strength 0.85, Lab, measured palette, saturation 1.1, the rest
  neutral). `settings.json` (`PUT /api/settings`) is validated without the
  dither registry, so it can name a method this build lacks: that knob
  alone falls back to the built-in default's value, and the stored
  default's other knobs still apply, as for an override. Each such warning
  is logged once.
- **Override** (`photos.recipe`): stored complete. `PUT /recipe` merges a
  partial body over the photo's effective recipe before storing it. A body
  with no recipe fields (`{}`, `{"reset": false}`) changes nothing, so a
  photo that follows the default is not pinned to today's default by a UI
  that always sends `reset`.
- **Effective recipe** = the override merged over the default (so a recipe
  stored by an older version gains new knobs), or the default when there is
  no override. A stored value that no longer validates (a method dropped
  from the registry, a range that narrowed, a retired knob) is replaced by
  the default's value **for that knob alone**: the override's other knobs
  still apply, and a later partial `PUT` does not lose them.

## Render keys

```
key = sha256(canonical JSON of {
        "format": FORMAT_VERSION,            # pipeline output version
        "engine": engine_version(),          # "<ABI>:<sha256 of dither.c + blue_noise_64.npy>"
        "recipe": complete_recipe(recipe),   # normalised: 1 == 1.0, any key order
        "dither_palette": [[r, g, b] x 6],   # the palette actually dithered against
        "png_palette":    [[r, g, b] x 6],   # the measured palette colouring the PNG
     })[:16]
```

Equal inputs give equal keys whatever the dict order. Any knob, either
palette, the format or the engine changes the key (tested for every knob).
`dither.ABI_VERSION` alone is the ctypes calling convention, not an output
version, so the engine part also digests `dither.c` (kernels, Lab tables)
and the blue-noise texture: a change to either changes every key, and old
renders, which are served with a one-year immutable `Cache-Control`, are
never served for the new output. The Pi rebuilds the library whenever
`dither.c` changes (`install.sh --update` from the post-receive hook).

The master is not part of the key: renders live in a per-photo directory,
a photo's renders are deleted with it, and masters never change (`ingest`
refuses to overwrite one). The fit LRU still tracks the master file's mtime
and size, as a second line of defence.

**Bump `pipeline.FORMAT_VERSION`** whenever the output for an unchanged
recipe changes for a reason the key cannot see: a different fit or
resampling, a new adjust stage, a different PNG layout, or an output change
on the Python side of the engine (`dither.py`). Every key then changes and
photos re-render lazily.

## Cache

```
<data>/renders/<photo_id>/<key>.png
```

- A 600 x 400, 4-bit, six-entry paletted PNG, about 60 to 100 KB.
- **The pixel values are the frame**: palette indices 0..5 in the order
  black, white, yellow, red, blue, green. The colour table is the measured
  palette, so a browser shows what the panel shows. That mapping is
  informational only: `RenderCache.get` returns the indices and ignores the
  colours.
- Both reads validate the file: `get` and `read_png` (what the API serves)
  check that it decodes to a 600 x 400 frame whose indices are all below 6.
  Anything else is a miss, and the file is deleted.
- Writes are atomic (`paths.atomic_write_bytes`). Reads tolerate a missing
  file or directory, and ids and keys are validated before they become paths.
- No `.bin` of packed bytes is stored (plan section 5 mentions one). Packing
  depends on `display.rotation` and is cheap numpy work, so the display
  driver packs at show time.

**Invalidation**: there is none to do on a recipe or palette change, because
the key changes and stale files are simply never requested again.

- `prune(photo_id, keep_keys)` keeps at most 40 renders per photo. The
  oldest-written go first, but never the keys in `keep_keys` (the photo's
  current render and the preview set just made). It also sweeps abandoned
  atomic-write temp files older than 10 minutes. It runs after every preview
  set and recipe change.
- `invalidate(photo_id)` removes the photo's directory on delete.

**Fit LRU**: `pipeline.load_fit` memoises the 4 most recent fits (720 KB
each), keyed by photo id, master path, mtime and size. A preview set, or a
preview followed by a save, decodes and resizes the master once.

`RenderCache.render_or_get(photo, recipe, settings)` (recipe `None` means the
effective recipe) is the one entry point that the API, the upload path and
the photos app all use. `render_methods` makes the editor's 12-method strip:
it adjusts the master once and dithers 12 times, and a cached method costs
one read and decode of its PNG (about 1 ms on the desktop). A corrupt or
foreign file under a preview key is removed and rendered again: a preview
key is not the photo's current one, so its URL could not re-render it.

The photos app (`apps/photos.py`) needs the global `Settings` snapshot as
`RenderContext.app_settings`, since it picks the recipe and palette; anything
else is an `AppError`, never a quiet render with default settings under keys
the Photos page never shows.

## Threading

Everything that decodes, resizes, adjusts, dithers or encodes a PNG runs on
`app.state.render_executor`: one thread, shared with the scheduler, so the Pi
never runs two dithers at once. Async routes `await
loop.run_in_executor(...)` through `api.run_on_render_executor`:

| Route | Event loop | Threadpool | Render executor |
|---|---|---|---|
| `POST /api/photos` | wait for an upload slot, stream and parse the multipart body | spooled-file IO | ingest, DB insert, default render |
| `GET /api/photos`, `GET /{id}`, `PUT /{id}/flags` | | DB, key hashing, `stat` | |
| `GET /{id}/render` (with or without `?recipe`) | | DB, key hashing, read and validate a cached PNG | render on a miss |
| `POST /{id}/previews`, `PUT /{id}/recipe` | | | all of it |
| `DELETE /{id}` | | | master, renders, then the row |

A cached render is always served from the threadpool, so polling the current
render (`GET /{id}/render` without a key) never queues behind a preview set.
Deletes go through the executor too. A render already queued for the photo
then runs first and cannot recreate files after they were removed, and a
render queued later finds the row gone.

**Upload slots**: at most `api.photos.UPLOAD_SLOTS` (2) uploads hold their
bytes at once: parsing, waiting for the executor, ingesting. The UI uploads
two files in parallel, so the second one's transfer overlaps the first one's
ingest; any further upload waits before its body is read, so memory stays
bounded however many requests a client sends. A slot holder whose body stops
arriving for 30 s (`UPLOAD_IDLE_SECONDS`), or is still arriving after 300 s
(`UPLOAD_READ_SECONDS`, 25 MB at 85 KB/s), gets a 408 and its slot back, so
two stalled clients cannot block every other upload (uvicorn has no
body-read timeout). The partly spooled file is closed whenever the body is
cut off (413 or 408): Starlette 1.7's `MultiPartParser.parse` closes every
file it opened on any exception, and a test pins that.

Once the row is inserted, an upload answers 201 even if the default render
then fails (a full disk, a `MemoryError`): the photo is stored and renders on
demand later, and an error would make the client retry and store it twice.

## API

All under `/api`. Errors are `{"error": "<message>"}`. Ids are 32 lowercase
hex characters; anything else is a 404.

The photo object is `Photo.to_dict()` without `master_path`, plus
`recipe_effective`, `render_key`, `render_url`
(`/api/photos/<id>/render?recipe=<key>`) and `render_ready`.

| Endpoint | Body | Response |
|---|---|---|
| `GET /photos` | | `{"photos": [photo...], "count": n}`, newest first |
| `POST /photos` | multipart `file` (one), optional `name` | 201 photo (its default render cached unless that failed: `render_ready`). 413 / 415 / 422, 408 for a body that stalls |
| `GET /photos/{id}` | | photo |
| `GET /photos/{id}/render?recipe=<key>` | | `image/png`, `Cache-Control: public, max-age=31536000, immutable`. A missing key is rendered when it is the current one, otherwise 404. Without `recipe`: the current render, `no-cache` |
| `POST /photos/{id}/previews` | any of the recipe knobs except `method` (which is ignored) | `{"previews": [{"method", "name", "description", "kind", "key", "url"} x 12], "took_ms": n}` in registry order |
| `PUT /photos/{id}/recipe` | full or partial recipe, or `{"reset": true}`; no fields changes nothing | photo, rendered |
| `PUT /photos/{id}/flags` | `{"excluded": bool}` | photo |
| `DELETE /photos/{id}` | | 204 |
| `GET /recipe/default` | | the complete default recipe, plus `methods: [{id, name, description, kind}]` and `limits: {knob: {min, max} or {choices}}` |
| `PUT /recipe/default` | full or partial recipe (`methods` and `limits` are ignored) | same shape as `GET`. Existing photos re-render lazily |

## Limits

| What | Value | Where |
|---|---|---|
| upload size | 25 MB (plus 64 KB multipart allowance) | `masters.MAX_UPLOAD_BYTES`, `api.photos.MAX_UPLOAD_BYTES` |
| uploads holding bytes at once | 2 | `api.photos.UPLOAD_SLOTS` |
| image size | 60 MP | `masters.MAX_PIXELS` |
| RLE-compressed BMP | 4 MP | `masters.MAX_RLE_BMP_PIXELS` |
| header parsing steps (JPEG markers and padding, PNG chunks, GIF blocks) | 50 000 | `masters.MAX_HEADER_STEPS` |
| GIF comment joining | 256 MB copied (about a 360 KB comment) | `masters.MAX_GIF_COMMENT_COPY` |
| upload body: no data / in all | 30 s / 300 s, then 408 | `api.photos.UPLOAD_IDLE_SECONDS`, `UPLOAD_READ_SECONDS` |
| ingest memory: estimate plus the upload's bytes | 150 MB | `masters.MAX_DECODE_BYTES` |
| master | long edge 1800 px, JPEG q92 | `masters.MASTER_LONG_EDGE` |
| original name | 120 characters | `masters.MAX_NAME_CHARS` |
| renders per photo | 40 | `cache.MAX_RENDERS_PER_PHOTO` |
| fit LRU | 4 entries | `pipeline.FIT_CACHE_SIZE` |
| files per upload request | 1 | multipart parser |

## Memory

The Pi Zero 2 W has about 415 MB. The plan's target is under 250 MB RSS.
The budget is derived from it: the app's baseline is about 62 MB (working
set measured on the desktop), and the one other upload slot can hold up to
25 MB of bytes, which leaves about 160 MB for one ingest including its own
upload bytes; `MAX_DECODE_BYTES` is 150 MB to keep some slack. Typical
uploads are far below it: the web UI downscales in the browser to an 1800 px
JPEG of a few hundred KB (Safari decodes HEIC itself), so only a HEIC from
desktop Chrome, or an original PNG, TIFF or WebP, reaches the expensive
paths.

**The model** (`masters.decode_cost`) is built from the buffers the code
allocates. The peak is the largest of four stages, each counting what is
alive at once: the decode (Pillow's image plus the decoder's own state: a
progressive JPEG's or MPO's coefficients, a compressed TIFF's strip buffer,
the TIFF plugin's in-place transpose, libwebp's two RGBA canvases and frame
copy, libheif's YCbCr planes, and the copies Pillow makes while it reads a
PNG text chunk), the strip-wise normalise (decoded image plus the reduced
copy), the resize (reduced image, Lanczos's intermediate, master) and the
tail (transpose, sRGB, JPEG encoder). What the header parser keeps
(`scan_metadata`: PNG text, JPEG APP segments) is added to every stage,
since every image derived from the opened one shares it through its copied
`info` until the master is encoded. The total is scaled by 1.1 and 6 MB is
added. `tests/test_masters_memory.py` measures real peaks in a fresh
interpreter and checks both that every estimate is an upper bound and that
a refused image is refused before it is decoded.

Measured on the desktop (Windows, Python 3.10, Pillow 12.3, pillow-heif
1.8.1): peak working set above the level just before `ingest`, against the
estimate including the file's bytes, and what the default budget does.

| Upload | Peak | Estimate | At 150 MB |
|---|---|---|---|
| 24 MP baseline JPEG (5712 x 4284), drafted to 1/2 | +51 MB | 62 MB | accepted |
| 48 MP baseline JPEG (8064 x 6048), drafted to 1/4 | +34 MB | 44 MB | accepted |
| 24 MP progressive JPEG, 4:2:0 | +99 MB | 115 MB | accepted |
| 17 MP progressive JPEG, 4:4:4 | +122 MB | 141 MB | accepted |
| 24 MP progressive JPEG, 4:4:4 | +173 MB | 197 MB | refused |
| 12 MP progressive MPO (JPEG with an MPF segment; was costed as baseline) | +49 MB | 60 MB | accepted |
| 48 MP progressive JPEG, 4:2:0 | +157 MB | 180 MB | refused |
| 24 MP RGB PNG | +108 MB | 125 MB | accepted |
| 30 MP RGB PNG | +135 MB | 154 MB | refused |
| 24 MP RGBA PNG (was +217 MB before strip-wise normalising) | +113 MB | 133 MB | accepted |
| 8 MP RGB PNG with 20 zTXt chunks inflating to 1 MB each and an 8 MB tEXt (93 MB without counting the text) | +92 MB | 133 MB | accepted |
| 0.8 MP PNG of 92 KB with 70 such zTXt chunks, at a 60 MB budget | +0.2 MB | 82 MB | refused before Pillow reads it |
| 60 MP RGBA PNG (7745 x 7745, a 1.4 MB file) | +261 MB if decoded | 295 MB | refused at +1 MB |
| 20 MP 16-bit grey PNG (was +221 MB) | +49 MB | 72 MB | accepted |
| 22 MP palette PNG with transparency (was +199 MB) | +38 MB | 52 MB | accepted |
| 6 MP WebP | +100 MB | 118 MB | accepted |
| 8 MP WebP | +135 MB | 159 MB | refused at +3.5 MB |
| 12 MP WebP (was accepted at about +200 MB) | +196 MB if decoded | 231 MB | refused at +3.5 MB |
| 24 MP deflate TIFF | +110 MB | 126 MB | accepted |
| 24 MP deflate TIFF with an orientation tag | +196 MB | 220 MB | refused |
| 48 MP deflate TIFF | +208 MB | 235 MB | refused |
| 12 MP HEIC (4032 x 3024) | +77 MB | 85 MB | accepted |
| 24 MP HEIC (5712 x 4284), strip path | +120 MB (+181 MB through the Pillow plugin) | 147 MB | accepted |
| 36 MP HEIC | +179 MB | 208 MB | refused |
| 12-method preview set | a few MB (fit 0.7 MB, prepared 0.7 MB, one 240 KB frame at a time) | | |

Every estimate in the calibration run (61 cases across all formats and
modes, plus the MPO and PNG-text cases above) was 11 to 77 % above its
measured peak; the tightest were the 12 MP HEIC and large deflate TIFFs. A refused image costs at most 3.5 MB
before the refusal (WebP's plugin copies the file at open). Roughly the
largest image each path accepts at the default budget:

| Path | Largest accepted |
|---|---|
| baseline JPEG | any up to the 60 MP limit (drafted) |
| progressive JPEG | about 40 MP at 4:2:0, 18 MP at 4:4:4 |
| RGB or RGBA PNG, compressed TIFF | about 28 MP |
| TIFF with an orientation tag | about 16 MP |
| palette, grey or 16-bit grey PNG | up to the 60 MP limit |
| WebP | about 7.5 MP |
| HEIC | about 25 MP (a 24 MP iPhone photo fits) |
| uncompressed TIFF and BMP | bounded by the 25 MB upload (8 MP RGB) |

A refused image gets a 422 naming its size and the memory it would need,
and suggesting a smaller or JPEG version.

Not counted in the estimate:

- the other upload slot's bytes (accounted for in the budget above). While
  that upload is being read, its spooled copy and its bytes coexist for a
  moment, since Trixie's `/tmp` is a tmpfs: 2 x 25 MB at the very worst;
- the process baseline;
- a TIFF's tag data (an XMP or ICC tag), which Pillow keeps while the
  opened image lives; it is bounded by the 25 MB file and is a few KB in
  real files.

The upload's bytes are counted once, also for compressed TIFFs: libtiff's
in-memory path decodes from `BytesIO.getvalue()`, and a `BytesIO` made from
`bytes` shares them rather than copying (tested with `tracemalloc`).

The factors were measured on the desktop only. glibc on the Pi allocates
differently, so run `pytest tests/test_masters_memory.py` there once (it
reads `VmHWM` on Linux) to confirm the estimates stay upper bounds.

## Timings (desktop, Python 3.10, MSVC build of the engine)

From `tests/test_api_photos.py::test_desktop_timings`, which prints them
every run. The first figure is warm, inside the full module run; the second
is the test run alone, including first-use costs such as loading the engine,
building the Lab tables and imports.

| Step | Time |
|---|---|
| upload 2400 x 1600 JPEG, including ingest and the default render | 97 ms / 187 ms |
| upload 2400 x 1600 PNG | 92 ms / 166 ms |
| upload 2400 x 1600 HEIC | 167 ms / 249 ms |
| fit (decode the 1800 x 1200 master, Lanczos to 600 x 400) | 17 ms / 23 ms |
| single render (adjust and atkinson/Lab) | 8 ms / 12 ms |
| 12-method preview set, cold | 131 ms / 269 ms |
| 12-method preview set, cached | 4 ms / 9 ms |

From the memory calibration: a 24 MP JPEG ingests in about 0.15 s, a 24 MP
HEIC in about 0.5 s. The Pi is roughly 5 to 10x slower; the plan's target is
a full preview set in under 3 s on the Pi.
