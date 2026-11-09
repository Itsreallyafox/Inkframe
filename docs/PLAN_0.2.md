# Inkframe 0.2: proposal

Draft for Adam, 2026-11-07, with his decisions folded in the same day (listed at the end).
Nothing here is built yet. Five changes, in the order I would build them, each with what
you will see, how it fits the code that exists, and what it touches.

The constraints that shape every item: the panel-care rules R1-R7 stay as they are (a card
or photo is never on the panel for less than 3 minutes), apps keep the `App` protocol in
`inkframe/apps/base.py` (settings schema, variants, a card for every failure state), no new
Python dependencies, Python 3.10 and 3.13, and the current 4-inch frame keeps working
through each deploy.

---

## 1. Rotation rework: "N photos, then the cards"

**Today.** `rotation` is a list of up to 32 slots `{app_id, duration_minutes, options}`;
the photos slot has `dwell_minutes` and `order`. Planning what shows inside a 20-minute
cycle means juggling durations against each other, and the photo share is invisible.

**Model (decided).** One photo stream and one app list; the scheduler alternates between
them. A *cycle* is `in_a_row` photos, then every enabled card in the app list in order,
then photos again. Adam's choice: all the cards in a row, so none is missed while they go
by.

```
rotation:
  photos:
    in_a_row: 6           # photos shown back to back before the cards
    minutes_each: 3       # >= refresh floor (3 min)
    order: shuffle        # or sequential
  apps:                   # drag to reorder; every enabled app shows once per cycle
    - {app_id: weather,   enabled: true, minutes: 3}
    - {app_id: headlines, enabled: true, minutes: 3}
    - {app_id: stocks,    enabled: true, minutes: 2}
    - {app_id: countdown, enabled: true, minutes: 2}
    - {app_id: trivia,    enabled: true, minutes: 2}
    - {app_id: quotes,    enabled: true, minutes: 2}
    - {app_id: patterns,  enabled: true, minutes: 2}
```

With the example above a cycle is 34 minutes: 18 minutes of photos (53 percent) and 16 of
cards, and every card comes back once per cycle. Raising `in_a_row` to 12 makes a
52-minute cycle at 69 percent photos. The Rotation page shows the cycle length, the photo
share and "cards every N minutes" live as you edit, plus a timeline strip of the next
hour, so the trade-off is visible instead of implied. Turning an app off removes it from
the cycle. There is no per-app frequency knob: with all cards in a row it would add little,
and it can come later if a filler app ever feels too frequent.

**What stays.** Quiet hours, pause/resume, show-now, show-next, keep-alive, the floor,
clock-jump handling, the `/api/status rotation{...}` shape that `now.js` reads, and the
per-photo exclusion flag.

**Code.** `config.py` (new `RotationSettings` with `PhotoStream` and `AppTurn`; the old
slot list is accepted by `from_dict` and converted once, so an existing `settings.json`
keeps working: the photos slot's `duration / dwell` becomes `in_a_row`, app slots keep
their minutes and order), `scheduler.py` (`_plan`, `SchedulerState` gains a cycle
position: photos shown this cycle, index of the card being shown; `show_next` advances
within the same model), `api/rotation.py` (new shape, 422 names the field), the whole of
`static/js/rotation.js` (551 lines today: a photo panel, a sortable app list with
enable/minutes, the live numbers and the hour strip), docs/SCHEDULER.md tables and
timelines, README settings table, the scheduler and API tests (fake-clock timelines for
the cycle model, the settings migration, the UI budget).

**Size.** The largest item: core logic plus a UI page. About the size of Phase 6.

---

## 2. Countdown app

**What you will see.** A card counting the days to the next event, with a designed title
card per holiday: New Year's (fireworks and the new year's number), Valentine's Day
(hearts), St. Patrick's Day (shamrocks), Easter (a decorated egg), Memorial Day (a flag),
4th of July (stars and fireworks in red and blue), Labor Day (a banner), Halloween (an
orange jack-o'-lantern), Thanksgiving (leaves, a pie), Christmas (a tree with red and
yellow baubles), a birthday with your name (cake and balloons), and a neutral confetti
card for a custom date. Mother's Day and Father's Day are left out on purpose. On the day
itself the card celebrates ("Merry Christmas!" with no number); from the next day the
count moves to the next occurrence. Three layouts rotate by the hour like Weather: a
poster with the number very large, a tear-off calendar page, and a banner with the motif
across the top.

**Art (decided).** The motifs are drawn in full colour and dithered like a photo, so the
pumpkin is orange and the egg can have pastel bands; the app sets `needs_dither=True` and
returns RGB, which the existing `render_to_frame` path handles. Type and the big number
stay crisp: they are composited after the dither in palette black.

**Behaviour.** Default is automatic: the nearest upcoming event among the built-ins (and
the birthday, when one is set). A custom date, when set, takes over until it passes, then
the automatic pick resumes. Dates are computed in the frame's local time: New Year 01-01
of the coming year, Valentine's 02-14, St. Patrick's 03-17, Easter by the Gregorian
computus (Western date), Memorial Day the last Monday of May, Independence Day 07-04,
Labor Day the first Monday of September, Halloween 10-31, Thanksgiving the fourth
Thursday of November, Christmas 12-25. "Days until" counts calendar days, so 23:59 the
night before still says 1.

**Settings** (schema-driven form, no UI work): `event` (select: automatic, each holiday,
birthday, custom), `holidays` (list of the built-ins the automatic pick may choose;
default all), `birthday` (text `MM-DD`), `birthday_name` (text), `custom_label` (text),
`custom_date` (text `YYYY-MM-DD`, or `MM-DD` for a yearly date), `layout` (rotate or one
of three). Bad dates render a "check the date" card, never an error.

**Code.** New `inkframe/apps/countdown.py` (dates incl. the Easter computus, settings, the
automatic pick), `countdown_art.py` (twelve motifs drawn in RGB with Pillow),
`countdown_layouts.py`; `APP_IDS`; README app table; docs/previews; tests with a fake
clock across year boundaries, leap days, the day-of and day-after cases, Easter dates
against a known table, and every motif rendered. No network, so it is also the card that
always works.

**Size.** Medium; most of the effort is twelve pieces of art.

---

## 3. Trivia app (fun facts alongside Quotes)

**What you will see.** A separate app, so it gets its own place and timing in the
rotation, with the same typographic quality as Quotes. Three layouts: a "Did you know?"
poster, an "On this day" card (two or three dated events for today's date), and an index
card with a category label and a source line.

**Sources.**

- Bundled: a new `inkframe/apps/data/trivia.json` with about 300 facts written for the
  frame, each with a category (science, space, nature, animals, history, geography,
  language, numbers, food) and a one-line source note. Works offline, no attribution
  problems. Facts are not copyrightable, but the wording will be our own.
- Wikipedia "On this day" (Wikimedia REST API, free, no key, our User-Agent): events and
  births for today's date, cached for the day, with the "Wikipedia · CC BY-SA" line the
  licence asks for. Offline or unreachable: the bundled set fills in, like Quotes falling
  back from ZenQuotes.

Decided: both sources, default `mix`, so the card works offline and is fresh online.

**Settings:** `source` (bundled, on this day, mix), `categories` (list), `show_source`
(bool), `layout`.

**Code.** `inkframe/apps/trivia.py`, `trivia_data.py` (selection with a per-day seed so
the same fact does not repeat within a day, the Wikipedia fetch with the retry we added,
the day cache), `trivia_layouts.py` reusing `quotes_type.py` where it fits; `APP_IDS`;
README; previews; tests with fixtures for the Wikipedia response and no network.

**Size.** Small. The data file is the longest part.

---

## 4. Stocks app

**What you will see.** A clean white card, modern and quiet: ticker, last price, day
change in green or red (two colours the panel already has), and a thin one-month
sparkline per symbol. A status line says when the data is from ("Fri close" at the
weekend). Three layouts sized for Adam's ten symbols: a board (two columns of up to six
rows), a hero (the three indices large with wide charts, the stocks in a strip below), and
a 3x4 grid of tiles.

**Data.** Default: Yahoo Finance's public chart endpoint (`query1.finance.yahoo.com/v8/
finance/chart/SYMBOL?range=1mo&interval=1d`): price, previous close and the closes for
the sparkline in one request per symbol, no key, works for stocks, indices (`^GSPC`) and
crypto (`BTC-USD`). It is unofficial, so the app caches 15 minutes, retries once, and
shows the last data with a "couldn't update" stamp when it fails. Fallback provider:
Stooq's free daily CSV (delayed, no key). No key-based provider in this round; the free
tiers that need a key allow too few calls for a frame. Decided: Yahoo as the default.
*Built (step C, 2026-11-09): Stooq is no longer a fallback; its CSV URL now sits behind a
JavaScript browser-verification challenge, so the app degrades to its cache and then an
"unreachable" card. The retry goes to Yahoo's `query2` host instead.*

**Defaults (decided):** `AAPL, NVDA, GOOG, ^GSPC, ^IXIC, ^DJI, T, F, META, AMD`, with the
indices labelled S&P 500, Nasdaq and Dow. Ten symbols is one request each per refresh,
cached 15 minutes, so about 40 requests an hour at most.

**Settings:** `symbols` (list, up to twelve), `labels` (list of `SYMBOL=Name`, optional),
`range` (1 week, 1 month, 3 months), `show_percent` (bool), `layout`.

**Code.** `inkframe/apps/stocks.py`, `stocks_data.py` (fetch, parse, cache, provider
switch), `stocks_layouts.py` (sparkline drawn with `Canvas` lines, palette-pure);
`APP_IDS`; README; previews; tests with recorded responses, market-closed and
half-missing data, and the unreachable card.

**Size.** Small to medium.

---

## 5. Cropping in the photo editor

**What you will see.** A "Crop" tool in the editor: a box over the photo locked to the
panel's 3:2 ratio that you drag and resize, so a portrait or square photo fills the panel
with the part you chose instead of white bars. "Reset to fit" brings the bars back. The
preview re-renders as you adjust, like the recipe sliders, and the Photos grid shows a
small "cropped" badge. Decided: opt-in per photo, fit-with-bars stays the default, no
rotate tool.

**Design.** The crop is a per-photo attribute, not a recipe knob: it is geometry, it has
no meaning for the default recipe, and the ten recipe keys stay a contract. It is stored
on the photo row (a `crop` JSON column via a new entry in `db.MIGRATIONS`) as fractions of
the master (`{x, y, w, h}` in 0..1), so it survives any re-encode. `pipeline.fit_image`
gains the crop: crop, then resize to exactly 600x400; with no crop the letterbox fit
stays byte-for-byte what it is today. The crop joins the render-cache key so an old render
is never served for a new crop. The box is also applied to previews and `render_methods`.

**Code.** `db.py` (migration, `Photo.crop`, `set_photo_crop`), `render/pipeline.py`
(`fit_image(master, crop)`, `recipe_key` input), `render/cache.py`, `api/photos.py`
(`PUT /api/photos/{id}/crop`, validation: inside the image, 3:2 within a tolerance,
minimum size), `api/recipe.py` previews take the crop, `scheduler.invalidate(photo_id)`
on save, `static/js/editor.js` (the crop box: pointer events, aspect lock, keyboard
nudges, touch), `photos.js` badge, docs/PIPELINE.md, docs/UI.md, README, tests (fit
geometry, cache key, API validation, migration, a UI budget check).

**Size.** Medium. The box widget is the bulk of it; everything else is plumbing.

---

## Build plan

Order: rotation first (so the new apps land in the new model and the Rotation page is
written once), then the three apps (independent, so they can be built in parallel), then
cropping (independent of everything else).

| Step | Scope | Relative size | Deploy |
|---|---|---|---|
| A | Rotation rework, settings migration, Rotation page, docs | large | yes, after review |
| B | Countdown (twelve dithered motifs) + Trivia (parallel builders) | medium | yes |
| C | Stocks | small-medium | yes |
| D | Crop | medium | yes |

Each step: build, adversarial review against the acceptance criteria in this document, fix,
ruff and the full suite, commit, deploy to the Pi, a look at the panel. Each adds a
CHANGELOG entry under 0.2.

**Usage.** The thorough shape we used before (one builder, three reviewers, a fix loop, a
verifier) spent about 1.3M tokens on the retry fix. For 0.2, step A deserves that shape;
B to D can run leaner (one builder, one reviewer, one fix pass) at roughly half the cost.
Tell me which shape when you start, and whether to run steps back to back or one at a
time.

**Risks.** Yahoo's endpoint is unofficial and can change or rate-limit (the app degrades
to its cache, and Stooq is the fallback). The rotation migration must read every existing
`settings.json` shape: the conversion gets its own tests. The crop and the render cache
share a key: a mistake there serves stale renders, so the key change is reviewed on its
own.

---

## Decisions (Adam, 2026-11-07)

1. **Rotation.** After N photos, all enabled cards in a row, so nothing is missed while
   the cards go by. No per-app frequency knob.
2. **Countdown.** Dithered full-colour art. Birthday day and name are settings. Most major
   US holidays including Easter (a decorated egg); not Mother's Day or Father's Day.
3. **Stocks.** Yahoo's keyless endpoint as the default. Symbols: AAPL, NVDA, GOOG,
   S&P 500, Nasdaq, Dow, T, F, META, AMD.
4. **Trivia.** Both sources, so it works offline too.
5. **Crop.** Opt-in per photo; no rotate.

Still to decide when the build starts: the workflow shape for each step (thorough or
lean), and whether to run the steps back to back or one at a time.
