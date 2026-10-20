# Scheduler

`inkframe/scheduler.py` is the only code that drives the panel. It decides what
the frame shows and when. It also enforces the panel-care rules from plan
section 2 in code, whatever the settings say. This note covers the decision
order, the state it keeps, and the timing rules, with a few worked timelines.

## The rules, and where they live

| Rule | What it means | Where |
|---|---|---|
| R1 floor | Two refreshes are never closer than `panel.refresh_floor_seconds` (default 180 s, never below 30). The gap is measured from the **end of the last attempt**, on the wall clock and on the monotonic clock (see Clock safety), and a failed attempt counts. A lost attempt anchor (a corrupt state row) falls back to the last refresh, so a restart never drops the floor. | `_attempt_anchor`, `_floor_at`, `_plan` steps (b) and (e), `_load_state` |
| R2 show now | The only thing that may bypass R1. It still waits `panel.show_now_min_seconds` (default 30) after the last attempt, measured the same way. Only one request is pending; a newer one replaces it. | `show_now`, `show_next`, `_pending_fires_at`, `_plan` step (a) |
| R3 keep-alive | The panel refreshes at least every `panel.keepalive_hours` (default 20), also while paused or in quiet hours, by re-showing the frame already on it. The rotation does not move. The keep-alive is a refresh, so it waits for the floor; the settings therefore reject a floor longer than the keep-alive interval (`refresh_floor_seconds <= keepalive_hours * 3600`), which would otherwise postpone it. | `_keepalive_deadline`, `_keepalive`, `_plan` step (b), `PanelSettings` |
| R4 power | Every refresh is `init()`, then `clear()` if `display.clear_before_refresh`, then `show()`, with `sleep()` in a `finally`. Nothing else in the process touches the panel; previews render to PNG only. | `_refresh` |
| R5 quiet hours | No scheduled rotation refreshes and the last image stays. Keep-alive and show-now still work. | `quiet_window_active`, `_plan` step (d) |
| R6 robustness | A render that raises (any `BaseException`, `SystemExit` included) or returns an invalid frame is logged with its traceback and its slot is skipped before the panel is touched; the next slot is tried a second later. A render that times out, and a photos slot whose photo list cannot be read, are skipped the same way. If every slot fails, the scheduler waits 60 s. A display error is logged and counts as an attempt; afterwards the panel's content counts as unknown, so the next rotation change repaints it even with the same image. A failing database read never stops planning, so the keep-alive still fires. The thread never dies and the panel is never blanked. | `_show_item`, `_render`, `_skip_after_failure`, `_db_read`, `_refresh`, `_run` |
| R7 persistence | Rotation position, timing anchors, recent photos and the last frame survive a restart. Unreadable state, even a row that is not JSON, gives defaults instead of stopping the boot. | `SchedulerState`, `_load_state`, `_read_state`, `_save`, `_persist_frame` |

## Threading model

- `start()` spawns a daemon thread named `scheduler`. It runs one loop:
  `delay = tick(now_fn())`, then `wake_event.wait(delay)`, then clear the
  event. If `tick` raises, which would be a bug, the error is logged and the
  loop retries in 30 s. That includes a `SystemExit`, which `threading`
  would otherwise end the thread with, silently.
- `tick(now)` makes one decision and performs **at most one** panel refresh,
  synchronously. A real refresh takes about 19 s. `tick` returns how many
  seconds to sleep, clamped to 0.5–300 s. The 300 s cap means a settings edit
  made outside the API, a clock change or a DST night is noticed within five
  minutes.
- `wake()` sets the event so the loop looks again at once. `show_now`,
  `show_next`, `pause`, `resume`, `PUT /api/settings` and `PUT /api/rotation`
  all call it.
- A state lock (`RLock`) guards the state. It is held only while deciding and
  recording, **never during a render or a refresh**, so `snapshot()` (polled
  by `/api/status`) never waits on the panel. A second lock makes `tick`
  calls run one at a time.
- Renders go through the app's one-thread render executor, the same one the
  preview endpoint uses. This keeps Pillow fonts and the dither engine
  single-threaded, and avoids two 600 x 400 renders in RAM at once on the Pi
  Zero. If a render takes longer than 180 s, the scheduler stops waiting for
  it and counts it as a render failure (`RenderTimeout`). A timed-out job
  that has not started yet (queued behind a preview, say) is cancelled. One
  that is running cannot be stopped, so until it finishes every scheduler
  render fails at once instead of queueing behind it. No backlog of
  abandoned renders builds up, and the rotation falls into the usual 60 s
  backoff. The keep-alive re-show needs no render, so a hung app never
  delays it.
- `stop(timeout)` sets a stop flag, wakes the thread and joins it. A refresh
  that has already started finishes, because interrupting it could leave the
  panel powered. No new refresh starts once the flag is set; `_refresh`
  checks it before touching the panel. A render in flight does not hold
  `stop()` up: the scheduler waits for renders in 0.5 s slices and abandons
  one when the flag is set, without counting it as a failure (the rotation
  does not move).
- Shutdown (the lifespan) calls `stop(timeout=100)`, then `display.close()`
  (`sleep()` plus releasing the GPIO lines). 100 s covers a refresh with
  `clear_before_refresh` (two ~19 s refreshes, slower on a cold panel), and
  the systemd unit's `TimeoutStopSec=150` covers the stop. If the stop
  still times out mid-refresh (`Scheduler.refreshing`), the lifespan leaves
  the display alone: the driver's lock is free between `init()`, `clear()`
  and `show()`, so a `sleep()` from the lifespan could slip in after
  `clear()`, make `show()` fail and leave the panel white. The refresh's own
  `finally` puts the panel to sleep, and the unit's `ExecStopPost` drives
  the panel's pins low after the process exits. `sleep()` runs once per
  refresh inside the scheduler, and once more in `close()` at shutdown.

## Decision order inside `tick`

`_plan(now, settings)` returns one of `wait(until)`, `pending`, `keepalive` or
`rotate(item)`:

1. **(a) Show-now due.** A request is pending and
   `now >= last_attempt + show_now_min_seconds` (or nothing was ever
   attempted). Fire it. Otherwise its `fires_at` becomes one of the wake-up
   times.
2. **(b) Keep-alive due.** `now >= keepalive deadline`. The deadline is
   `last_refresh_at + keepalive_hours`. If no successful refresh is recorded,
   it is "now" when a last frame exists on disk, and otherwise
   `created_at + keepalive_hours`.
   - If the floor has not passed since the last attempt, wait for it. A
     keep-alive is a refresh, so R1 applies.
   - If the rotation is allowed (not paused, not quiet, no backoff) and is due
     right now, do the rotation refresh instead. It refreshes the panel anyway
     and moves on. If that rotation item is the photo already on the panel,
     which would not refresh it, its timers restart and the last frame is
     re-shown in the same tick, so the keep-alive is never put off.
   - Otherwise re-show the last frame, from memory or from
     `last_frame.bin`.
   - If there is no frame at all, render the current slot's content without
     advancing the rotation, and use that. In a photos slot whose photo is
     gone (deleted, or the slot showed the "no photos" card and photos have
     been added since), that is the slot's next photo, shown as a photo
     change inside the slot. If that render fails, the rotation
     pointer moves on to the next slot as for any render failure, also while
     paused or in quiet hours. This is the one case in which a keep-alive
     moves the rotation: it walks on to a slot that renders instead of
     retrying a broken one until the panel is overdue.
3. **(c) Paused.** Wait for the earlier of the keep-alive deadline (never
   before the floor) and the pending show-now.
4. **(d) Quiet hours.** The same as paused, but the wait also ends when the
   quiet window ends.
5. **Backoff.** After a pass in which every slot failed to render, wait until
   the 60 s backoff ends.
6. **(e)+(f)+(g) Rotation.** `target = max(rotation due time, last_attempt +
   floor)`. If `target <= now`, render the item and refresh. Otherwise wait
   until `target`, or until an earlier wake-up time (the pending show-now or
   the keep-alive).

After an action, `tick` plans again at the moment the action finished. The
returned delay is therefore the real time to the next event: for example the
floor after a refresh, 1 s after a render failure, 60 s for a backoff. After
a failed render the retry or backoff delay is only an upper bound: an earlier
event, such as a keep-alive coming due inside the backoff, wins.

### When the rotation is due

`_rotation_plan` ignores pause, quiet hours, backoff and the floor; `_plan`
applies those on top.

- **The slot has not shown its first frame yet.** This covers a fresh start, a
  slot that was disabled or removed, and the next slot after a render
  failure. The slot is due immediately.
- **App slot.** The slot ends at `slot_started_at + duration_minutes`. Then
  the next **enabled** slot is entered (cyclically, possibly the same slot
  again). Its app is rendered again, so a weather-only rotation updates.
- **Photos slot** (`options.dwell_minutes`, default 3; `options.order`,
  `shuffle` or `sequential`, default `shuffle`):
  - The photo changes at `photo_started_at + dwell`. A change is skipped if
    the next refresh after it could not come before the slot's end, that is
    if `max(change, floor_at) + floor > slot_end`. This stops a photo change
    from making the slot overrun.
  - With no included photos, the photos app renders its "no photos" card
    (`item_id=None`) and the slot lasts one dwell.
  - With one photo, or when the rotation loops back to the photo already on
    the panel, nothing is refreshed. The timers restart instead, so the panel
    does not flicker for an identical image. The exception is a **stale**
    panel (`panel_stale`): after a failed or interrupted attempt its content
    is unknown (white, if `clear()` worked and `show()` did not), and after
    `invalidate()` the photo would render differently (Phase 3: a new
    recipe). Then the next rotation change re-renders and refreshes even the
    same photo. A keep-alive re-show of the last frame leaves the flag set.
  - `sequential` walks included photos by `(created_at, id)`, cycling. It
    continues after the most recent photo picked, whether that photo was shown
    or failed, so a broken photo is walked past. If that photo was deleted, it
    continues after the newest earlier pick that still exists.
  - `shuffle` picks uniformly among included photos that are not among the
    last `min(n - 1, 20)` picks. It never picks the photo on the panel while
    another one exists. The pick is reserved (`next_photo_id`), so the "next"
    the UI shows is the photo that actually comes next.
- **Rotation edits.** The state remembers the slot's index **and** its app
  id. If the slot moves (slots inserted or reordered), the scheduler follows
  it by app id and keeps its timing. If it is disabled or removed, the next
  enabled slot starts fresh, at the floor. Slots whose app is unknown or
  failed to load are skipped. With no enabled slots, only the keep-alive runs.

### Show now

- `show_now(app_id=...)` or `show_now(photo_id=...)` queues `{kind, id}`.
  Unknown ids raise `KeyError`. Excluded photos may be shown explicitly.
- `show_next()` queues `{kind: "next"}`. When it fires, it shows exactly the
  item `snapshot()["rotation"]["next"]` announced, and the rotation advances
  as if that item had come up on schedule.
- If the requested app or photo has an enabled slot in the rotation, the
  rotation **jumps** there and starts that slot now, unless that slot is the
  one already running: an app then keeps the slot's timing, and a photo shown
  inside the running photos slot only restarts the photo's dwell. If it
  has no slot, the rotation timing is left alone, and the next rotation change
  comes when it was due anyway (and no earlier than the floor).
- `fires_at = max(now, last_attempt + show_now_min_seconds)`, with
  `last_attempt` measured on both clocks as for the floor. It is reported in
  `rotation.pending` for the UI countdown and is recomputed whenever an attempt
  happens in between. While a render or refresh is in flight, the reported
  `fires_at` is at least `now + show_now_min_seconds`, because the attempt in
  flight ends later than now. Once it has ended, the real time is reported.
- The request is kept in memory only, so a restart drops it. If its render
  fails, the error is recorded and the request is not retried.

### Quiet hours

`quiet_window_active(qh, now) -> (active, until)`. For each weekday `d` in
`qh.days`, a window starts on weekday `d` at `start`. It ends at `end` the same
day if `end > start`, and the next day otherwise. `now` is quiet when
`start <= now < end` for a window that started today or yesterday, so the
start is inside the window and the end is not.

Example: `23:00–07:00` with only Sunday listed is quiet from Sunday 23:00 to
Monday 07:00. Monday 23:30 is not quiet.

Times are compared on the wall clock of `now`'s own zone; `now_fn` returns
local time. On a DST night the returned `until` can be an hour early in real
time. The scheduler then wakes, finds the window still active, and waits
again, so the rotation resumes at 07:00 local. `until` is always after `now`.

`now_fn` may return a DST-aware time (`zoneinfo`). The scheduler takes every
reading at its UTC offset at that instant, which is what `local_now()`
returns. Python compares and subtracts two datetimes that share one
DST-aware `tzinfo` on the wall clock, so without this a DST change would look
like a one-hour clock jump. `rotation.next_at` checks quiet hours on the local
clock of `now`, also for anchors read back from disk, which are UTC.

### Failures (R6)

- **Render failure** (any exception, including an executor timeout, and any
  other `BaseException` such as a `SystemExit` from a library calling
  `sys.exit()`; only a real Ctrl-C on the main thread, in a test or a REPL,
  is let through). The error is logged with its traceback, `last_error = {at,
  kind: "render", app_id, message}`, and the slot joins this pass's "failed"
  set. The rotation points at the next enabled slot that has not failed and
  `tick` returns 1 s. The panel keeps its frame. When every enabled slot has
  failed, the set is cleared, a 60 s backoff starts, and the next pass begins
  with the following slot. Any successful refresh clears the set.
- **Invalid frame.** Every render result goes through `validate_frame` on
  the render thread, before the panel is touched. `render_to_frame` checks
  index arrays and undithered RGB, but passes the dither engine's output
  through unchecked; a bad frame from there (wrong shape, an index above 5)
  is a render failure, so its slot is skipped. Found only by
  `display.show()`, it would have been a display failure: the same item
  retried every floor forever, with a `clear()` before each attempt.
- **Display failure** (`DisplayError`, `DisplayTimeout`, `OSError`, or any
  other exception from the driver). `sleep()` still runs in the `finally`.
  The error is logged with its traceback and recorded as `last_error` with
  `kind: "display"`. The attempt counts for the floor and `last_refresh_at`
  is unchanged. The rotation does not advance, so the same item is retried
  after the floor. The panel is marked stale, so content that the rotation
  would otherwise skip as "already showing" (one photo, the "no photos"
  card) is repainted at the next rotation change instead of staying white
  until the keep-alive. A failed show-now is not retried. The last good
  frame is written back to `last_frame.bin` and `last_frame.png`, because a
  driver may have written something else there (`MockDisplay.clear()` writes
  white). If there is no good frame yet, whatever the driver wrote during
  the attempt is removed. A restart then keeps the right image alive.
- **Render timeout.** It counts as a render failure (`RenderTimeout`). While
  the timed-out render still runs, every render fails at once (see the
  threading model).
- **Unreadable database** (`sqlite3.Error`, or a row that does not parse).
  Planning and `snapshot()` never raise because of it. The first failure of
  each kind of read is logged with its traceback, and its recovery once. If
  the photo list cannot be read, the photos slot fails like a render that
  raised (`last_error.message` says why), so it is skipped and the other
  slots and the keep-alive carry on. Inside a running photos slot, the photo
  on the panel stays until the slot ends. `/api/status` reports an
  unreadable mirror key as the scheduler's own value, or `null`.
- Pausing, or quiet hours starting, while a slow render is in flight: the
  rendered frame is dropped and the panel is not refreshed.

### Clock safety (no RTC on the Pi Zero)

- **The floor and the show-now minimum use both clocks.** `_attempt_anchor`
  is `last_attempt_at`, moved later when `monotonic_fn` says less time has
  passed since this process's last attempt than the wall clock does. A
  refresh needs both clocks to agree that the floor (or the show-now
  minimum) has passed. No wall-clock step can shorten them, including steps
  of 60 s or less, which are not shifted (below). After a restart only the
  wall clock is known.
- **Jumps while running.** Every `tick` compares wall-clock progress, as
  instants in UTC, with `monotonic_fn`. A disagreement over 60 s, such as NTP
  stepping the clock after boot, shifts the timing anchors by the jump.
  Anchors this process set were taken on the clock as it was before the
  jump, so they move with it. A forward jump then cannot fake an overdue
  keep-alive, and a backward jump cannot stall the rotation. Anchors loaded
  from disk (or clamped at start-up) were written on a clock whose offset to
  this one is unknown. They move only in their safe direction: the
  keep-alive anchors (`last_refresh_at`, `created_at`) only earlier, and
  `last_attempt_at` only later. The slot and photo anchors and the backoff
  always move.

  Why: the Pi boots with the clock it saved before a power cut. Say the
  rotation is paused, the last refresh was at 12:00, the power fails at
  13:00 (the saved clock) and comes back at 22:00. The boot clock says
  13:00, then NTP steps it 9 h forward. The keep-alive stays due at 08:00,
  20 h after the real refresh. Shifting `last_refresh_at` with the step would
  have moved it to 17:00 the next day, 29 h after the refresh, and a longer
  outage would have pushed it further.
- **Restart with the clock behind.** Any anchor more than 60 s in the future
  is pulled back to "now". The scheduler then waits one full floor from start
  rather than waiting for a timestamp that may never come.

**Repair after a failed attempt.** If the last attempt came after the last success (it failed, so with `clear_before_refresh` the panel may be white), the keep-alive deadline is pulled forward to `last_attempt_at + max(floor, 300 s)`, paused or quiet or not. The re-show of the last good frame then repairs the panel at the next floor instead of up to `keepalive_hours` later; a broken panel retries at that slower pace (R1 holds).

## State (R7)

Database state keys:

| Key | Value |
|---|---|
| `scheduler` | `SchedulerState` as JSON (below) |
| `last_refresh_at` | ISO string; mirrors `SchedulerState.last_refresh_at`, written on every successful refresh (Phase 1's `/api/status` reads it) |
| `last_source` | `{type, id, label}`; mirrors `SchedulerState.last_source` |

`SchedulerState` fields. Times are ISO-8601 UTC with milliseconds, the db
module's convention. They are rounded up to the next millisecond, so a floor
read back after a restart is never short.

| Field | Meaning |
|---|---|
| `slot_index`, `slot_app_id` | rotation position: the index into `settings.rotation` and the app id it had when the slot started |
| `slot_started_at` | when the slot's first frame appeared; `null` means the slot has not shown yet, so it is due |
| `photo_id`, `photo_started_at` | the photo the photos slot is showing; the dwell anchor |
| `recent_photo_ids` | the last 50 photo picks (shown or failed), newest last |
| `next_photo_id` | the reserved shuffle pick |
| `last_refresh_at`, `last_attempt_at` | end of the last successful refresh and end of the last attempt. `last_attempt_at` is also written just **before** the panel is touched (after the render, not at the start of the tick), so a crash or power cut mid-refresh still counts from the right moment |
| `last_source` | what the last refresh showed: `{type: app\|photo\|keepalive, id, label}` |
| `showing` | the content on the panel, `{type: app\|photo, id, label}`. A keep-alive does not change it |
| `panel_stale` | the panel may not show `showing`: set when an attempt starts (so a crash mid-refresh leaves it set) and by `invalidate()`, cleared by a successful refresh of a fresh render (a keep-alive re-show keeps its value). While set, the rotation never skips the same content as "already showing" |
| `last_error` | the most recent failure: `{at, kind: render\|display, app_id, message}`. Never cleared; compare `at` with `last_refresh_at` |
| `frame_rotation` | the packing rotation `last_frame.bin` was written with, so a keep-alive after a rotation change unpacks the frame correctly |
| `created_at` | when this state was first created; the keep-alive anchor when nothing was ever shown |

After every successful refresh, the frame is written to `<data>/last_frame.bin`
(the controller bytes, from `pack_frame` with `display.rotation`) and to
`last_frame.png` (in the measured palette). Both are written atomically. A
restarted scheduler keeps the frame alive from `last_frame.bin`.

Loading is tolerant: a missing or invalid blob gives defaults. So does a row
that is not JSON at all (a torn write, SD-card bit rot) or a failing database;
the next save rewrites it. The two mirror keys fill gaps, `slot_index` is
clamped to the current rotation length, and a deleted current photo is
dropped. The paused flag lives in `settings.paused`, not here.

The floor and the show-now minimum must survive a lost anchor. A missing (or
unparsable) `last_attempt_at`, or one older than `last_refresh_at`, becomes
`last_refresh_at`, from the blob or its mirror key: an attempt ended at
least then. If neither time is known because the row itself was lost (not
JSON, not an object, unreadable) while `last_frame.bin` shows that the panel
has been driven, the start-up time stands in, so the first refresh waits one
floor. A row that is valid but has never recorded an attempt (a fresh state,
or a frame left by Phase 1) gets no stand-in.

## Timelines

Default floor 180 s; times are minutes after the first refresh.

**Photos 10 min (dwell 3), then Weather 3 min, two photos:**

```
 0:00  photo A        (slot starts; the floor runs to 3:00)
 3:00  photo B        (dwell)
 6:00  photo A
 9:00  -              (a change now could not be replaced before 12:00 > 10:00)
10:00  weather        (the slot's 10 minutes are up)
13:00  photo B        (weather's 3 minutes; back to photos)
```

**Quotes 2 min, Patterns 2 min, floor 180 s.** Slots shorter than the floor
last until the floor allows the next refresh: Quotes at 0:00, Patterns at 3:00,
the next slot at 6:00.

**Quiet hours 23:00–07:00, keep-alive 20 h, last rotation refresh 22:59:**
nothing between 23:00 and 07:00. At 07:00 the rotation resumes with the next
slot. With a 2 h keep-alive instead, the last frame is re-shown, byte for
byte, at 00:59, 02:59 and 04:59, and the rotation still resumes at 07:00 where
it left off.

**Show now at 0:10 after a refresh at 0:00:** `fires_at` is 0:30 (the show-now
minimum), not 3:00 (the floor). A second request at 0:15 replaces the first
and still fires at 0:30. Any later rotation refresh waits for the floor from
0:30.

**Display failure at 0:00:** the attempt counts, `last_refresh_at` is
unchanged, the next attempt is at 3:00 (never sooner), and the item is
retried then.

**One photo, Photos 10 min, `clear_before_refresh`, a show-now of that photo
fails at 0:05 after `clear()` worked:** the panel is white and stale. At
0:10 the slot's next visit would normally skip the same photo; it re-renders
and refreshes it instead, and the panel is repaired.

## API

All routes are JSON, and errors are `{"error": ...}`. Display routes reply with
`{"ok": true, "panel": {...}, "rotation": {...}}`, the scheduler's snapshot.

| Route | Does |
|---|---|
| `POST /api/display/next` | `show_next()` |
| `POST /api/display/pause` / `resume` | persist `settings.paused`, wake |
| `POST /api/display/show` | body `{"app_id"}` or `{"photo_id"}`; with `app_id` an optional integer `"variant"` forces one of the app's layouts (the Settings test card is `{"app_id": "patterns", "variant": 2}`). Unknown id: 404. Both, neither, extra keys, a non-string, or a variant that is out of range or given with `photo_id`: 422 |
| `GET /api/rotation` | `{slots: [{app_id, enabled, duration_minutes, options, name, description}], paused, quiet_hours, apps}`. The photos options are exactly `dwell_minutes` and `order`, as the scheduler reads them (defaults filled in); other keys that `/api/settings` stored are left out, so a GET body can be PUT back |
| `PUT /api/rotation` | `{"slots": [...], "paused"?}`. Replaces the whole list. App ids must be known; photos options are `dwell_minutes` (whole number, 1–1440) and `order` (`shuffle` or `sequential`). Everything is validated through `Settings.merged`, and `name`/`description` from the GET shape are ignored. Invalid input: 422 and nothing changes. Wakes the scheduler |
| `GET /api/apps` | `[{id, name, description, needs_dither, variants, settings_schema, settings}]` |
| `GET/PUT /api/apps/{id}/settings` | PUT takes a partial body, merges it over the stored settings and validates the result with the app's schema (422 with the message if it fails). Only keys the user set are stored, so a required field left unset, such as the weather location, keeps prompting |
| `GET /api/apps/{id}/preview.png?variant=&seed=` | renders on the render executor, never the panel, with the scheduler's `RenderContext`. Returns a PNG in the measured palette with `Cache-Control: no-store`. App failure: 500. Bad `variant` or `seed`: 422. The photos preview uses the current photo, else the first included photo |

`/api/status` keeps every Phase 1 key. It adds `panel.last_attempt_at`,
`panel.last_error`, `rotation.quiet`, `rotation.quiet_until` and
`rotation.pending`, and fills `rotation.current`, `rotation.next`,
`rotation.next_at` and `rotation.keepalive_at`:

```json
"panel": {"state": "asleep", "last_refresh_at": "2026-10-19T20:54:00.147+00:00",
          "last_attempt_at": "2026-10-19T20:54:00.147+00:00",
          "last_source": {"type": "app", "id": "photos", "label": "Photos"},
          "last_error": null},
"rotation": {"paused": false, "quiet": false, "quiet_until": null,
             "current": {"app_id": "photos", "label": "Photos", "photo_id": null,
                         "started_at": "2026-10-19T20:54:00.147+00:00",
                         "ends_at": "2026-10-19T21:04:00.147+00:00"},
             "next": {"app_id": "weather", "label": "Weather", "photo_id": null},
             "next_at": "2026-10-19T20:57:00.147+00:00",
             "keepalive_at": "2026-10-20T16:54:00.147+00:00",
             "pending": null}
```

`current` is `null` until the rotation's current slot has shown its first
frame. `next_at` is `null` while paused or when no slot is enabled.
`keepalive_at` is never earlier than the floor allows.

## Hooks for Phase 3

- `invalidate(photo_id=None) -> bool`: call it when the content on the panel
  would now render differently, such as after a recipe change for the photo
  on it. The next rotation change then re-renders and refreshes that photo
  instead of skipping it as "already showing". With `photo_id`, it only
  marks the panel if that photo is on it (and returns whether it did).
  Nothing is refreshed early; the floor and quiet hours apply as usual.
  It is wired: `PUT /api/photos/<id>/recipe` calls `invalidate(<id>)` once
  the override is stored or reset (`{"reset": true}`; a body without recipe
  fields changes nothing and calls nothing), and `PUT /api/recipe/default`
  calls `invalidate()` with no id (any photo that follows the default may
  render differently) and then `wake()`.
- `current_photo_id()`: the photo the photos slot is showing.

## Known limitations

- An NTP step followed by a restart before the next tick (within 300 s) is
  never folded into the persisted anchors, and the monotonic anchor dies
  with the process. Across that restart the floor or the show-now minimum
  can be short by the step. Within one process every step size is handled.
- `frame_rotation` and `last_frame.bin` are written in two steps. A crash
  between them (with `MockDisplay`, which writes the file during `show()`,
  the whole refresh; with the panel driver, a few milliseconds) after a
  rotation change makes the next restart's keep-alive draw the frame upside
  down.
- `show_now(photo_id=...)` reads the photo row on the API thread; a row that
  does not parse makes `POST /api/display/show` a 422 with the decoder's
  message (only a SQLite error is a 500).
- `render_to_frame` (`inkframe/apps/base.py`) still returns the dither
  engine's output unchecked. The scheduler validates every frame itself; the
  preview endpoint relies on the engine (Phase 2) returning valid frames.
- A settings file whose `refresh_floor_seconds` exceeds `keepalive_hours`
  fails validation on load, so it is moved aside to `settings.json.invalid`
  and the defaults are used, like any other invalid settings file.

## Environment

| Variable | Effect |
|---|---|
| `INKFRAME_SCHEDULER=off` (or `0`, `false`, `no`) | the `Scheduler` is built, so the API works, but its thread does not start. `create_app(scheduler=False)` does the same; the test suite sets both |
| `INKFRAME_OFFLINE=1` (or `true`, `yes`, `on`) | every scheduler and preview render gets `RenderContext.online = False`. Network apps use their cache or draw their offline card. The test suite sets it |

## Testing

`tests/test_scheduler.py` and `tests/test_quiet_hours.py` never start the
thread. They drive `tick(now)` with a fake clock (`tests/conftest.py`:
`FakeClock`, `Rig`, `FakeApp`, `RecordingDisplay`) whose monotonic clock follows
the same fake time, and assert on:

- the returned delays;
- `show_count` and the order of display calls;
- `last_frame.bin`;
- db state and `snapshot()`.

`Rig.run_until(t)` follows the scheduler's own delays, which is how the
"never closer than the floor" and "nothing all night" properties are checked.

`tests/test_scheduler_probe.py` holds the regression tests from the two
reviews. Each one failed against the reviewed code. Round 1 covers:

- a state row that is not JSON;
- the attempt pre-write after a slow render;
- small wall-clock steps against the floor and the show-now minimum
  (`JumpClock`, whose monotonic clock does not see the step);
- NTP's first step after a power cut;
- a failing photo list;
- a DST-aware zone (`EasternClock`, on `zoneinfo` or an equivalent
  fold-aware `tzinfo`);
- `next_at` across a restart;
- the frame file after a failed refresh;
- timed-out renders left queued.

Round 2 covers:

- an invalid frame from the dither path (a stand-in engine);
- a corrupt state row against the floor and the show-now minimum;
- an app render raising `SystemExit` on the real thread;
- a failed refresh of static content (one photo) with `clear_before_refresh`;
- a floor longer than the keep-alive interval.

The thread itself is covered by a start/wake/stop test, a "stop during a
refresh in flight" test and a "stop during a slow render" test using real
time and a `MockDisplay`. `tests/test_api_display.py` checks that shutdown
leaves a refresh that outlives the stop timeout alone.
