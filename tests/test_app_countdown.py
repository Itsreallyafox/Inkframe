"""The Countdown app: date rules, selection, settings, motifs, layouts."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from inkframe.apps import countdown as C
from inkframe.apps import countdown_art as A
from inkframe.apps import countdown_layouts as L
from inkframe.apps.base import App, FontLibrary, RenderContext, render_to_frame, validate_settings
from inkframe.display.palette import BLACK, MEASURED_DEFAULT, N_COLORS, NOMINAL, RED, WHITE

FONTS = FontLibrary()
#: US Eastern offsets either side of the November DST change
EST = timezone(timedelta(hours=-5))
EDT = timezone(timedelta(hours=-4))
NOW = datetime(2026, 11, 9, 9, 30, tzinfo=EST)


def _settings(**kw) -> dict:
    return validate_settings(C.SETTINGS_SCHEMA, kw)


def _ctx(tmp_path: Path, now: datetime = NOW, **kw) -> RenderContext:
    settings = _settings(**kw.pop("settings", {}))
    return RenderContext(
        settings=settings, now=now, cache_dir=tmp_path, fonts=FONTS, online=False, **kw
    )


def _pin(key: str) -> dict:
    """Settings that pin ``key`` with whatever date it needs."""
    extra = {
        "birthday": {"birthday": "11-22", "birthday_name": "Sam"},
        "custom": {"custom_date": "2027-04-03", "custom_label": "Our trip"},
    }
    return {"event": key, **extra.get(key, {})}


# --- date rules ------------------------------------------------------------------------


EASTER = {
    2000: (4, 23),
    2019: (4, 21),
    2024: (3, 31),
    2025: (4, 20),
    2026: (4, 5),
    2027: (3, 28),
    2028: (4, 16),
    2029: (4, 1),
    2030: (4, 21),
    2038: (4, 25),
}


@pytest.mark.parametrize(("year", "md"), sorted(EASTER.items()))
def test_easter_table(year: int, md: tuple[int, int]) -> None:
    assert C.easter(year) == date(year, *md)
    assert C.holiday_date("easter", year) == date(year, *md)


MOVABLE = {
    "memorial_day": {2024: 27, 2025: 26, 2026: 25, 2027: 31, 2028: 29, 2029: 28, 2030: 27},
    "labor_day": {2024: 2, 2025: 1, 2026: 7, 2027: 6, 2028: 4, 2029: 3, 2030: 2},
    "thanksgiving": {2024: 28, 2025: 27, 2026: 26, 2027: 25, 2028: 23, 2029: 22, 2030: 28},
}
MONTH = {"memorial_day": 5, "labor_day": 9, "thanksgiving": 11}


@pytest.mark.parametrize("key", sorted(MOVABLE))
def test_movable_holiday_tables(key: str) -> None:
    for year, day in MOVABLE[key].items():
        d = C.holiday_date(key, year)
        assert d == date(year, MONTH[key], day), (key, year, d)
        assert d.weekday() == (3 if key == "thanksgiving" else 0)


@pytest.mark.parametrize("year", range(2024, 2031))
def test_fixed_holidays(year: int) -> None:
    fixed = {
        "new_year": (1, 1),
        "valentines": (2, 14),
        "st_patricks": (3, 17),
        "independence_day": (7, 4),
        "halloween": (10, 31),
        "christmas": (12, 25),
    }
    for key, md in fixed.items():
        assert C.holiday_date(key, year) == date(year, *md)


def test_unknown_holiday_key_is_a_bug() -> None:
    with pytest.raises(ValueError, match="unknown holiday"):
        C.holiday_date("mothers_day", 2026)  # deliberately not a holiday here


def test_next_holiday_rolls_to_next_year_once_passed() -> None:
    assert C.next_holiday("christmas", date(2026, 12, 25)) == date(2026, 12, 25)
    assert C.next_holiday("christmas", date(2026, 12, 26)) == date(2027, 12, 25)
    assert C.next_holiday("new_year", date(2026, 11, 9)) == date(2027, 1, 1)
    assert C.next_holiday("new_year", date(2027, 1, 1)) == date(2027, 1, 1)
    assert C.next_holiday("new_year", date(2027, 1, 2)) == date(2028, 1, 1)


def test_leap_day_birthday_falls_on_feb_28_in_other_years() -> None:
    assert C.yearly_date(2027, 2, 29) == date(2027, 2, 28)
    assert C.yearly_date(2028, 2, 29) == date(2028, 2, 29)
    assert C.next_yearly(2, 29, date(2027, 2, 1)) == date(2027, 2, 28)
    assert C.next_yearly(2, 29, date(2027, 3, 1)) == date(2028, 2, 29)
    ev = C.choose(_settings(event="birthday", birthday="02-29"), date(2027, 2, 28))
    assert isinstance(ev, C.Event) and ev.today


# --- counting days ------------------------------------------------------------------------


def test_days_until_counts_calendar_days_at_2359_and_0000(tmp_path: Path) -> None:
    # the night before Christmas still says 1; midnight says 0 and celebrates
    settings = _settings(event="christmas")
    late = datetime(2026, 12, 24, 23, 59, tzinfo=EST)
    assert C.choose(settings, C.local_today(_ctx(tmp_path, late))).days == 1
    midnight = datetime(2026, 12, 25, 0, 0, tzinfo=EST)
    ev = C.choose(settings, C.local_today(_ctx(tmp_path, midnight)))
    assert ev.days == 0 and ev.today and ev.celebration == "Merry Christmas!"
    # the day after, the count moves to the next occurrence
    after = datetime(2026, 12, 26, 0, 0, tzinfo=EST)
    ev = C.choose(settings, C.local_today(_ctx(tmp_path, after)))
    assert ev.date == date(2027, 12, 25) and ev.days == 364


def test_days_until_across_dst_and_the_year_boundary(tmp_path: Path) -> None:
    # Halloween 2026 seen from the night before and the morning after the clocks go back
    settings = _settings(event="thanksgiving")
    before = datetime(2026, 10, 31, 23, 59, tzinfo=EDT)  # still summer time
    after = datetime(2026, 11, 1, 0, 0, tzinfo=EST)  # the clocks went back at 02:00
    assert C.choose(settings, before.date()).days == 26
    assert C.choose(settings, after.date()).days == 25
    # New Year's Eve: 23:59 says 1, midnight celebrates, the year number is the new one
    settings = _settings(event="new_year")
    eve = C.choose(settings, datetime(2026, 12, 31, 23, 59, tzinfo=EST).date())
    assert eve.days == 1 and eve.date == date(2027, 1, 1)
    day = C.choose(settings, datetime(2027, 1, 1, 0, 0, tzinfo=EST).date())
    assert day.today and day.date.year == 2027
    # a UTC "now" is converted to the frame's local date by the app, not truncated
    ctx = _ctx(tmp_path, datetime(2027, 1, 1, 3, 0, tzinfo=timezone.utc))
    assert C.local_today(ctx) == date(2027, 1, 1)


# --- selection -----------------------------------------------------------------------------


def test_automatic_pick_through_the_year() -> None:
    s = _settings()
    assert C.choose(s, date(2027, 1, 5)).key == "valentines"
    assert C.choose(s, date(2026, 12, 27)).key == "new_year"
    assert C.choose(s, date(2027, 3, 21)).key == "easter"  # the week before Easter 2027
    assert C.choose(s, date(2027, 3, 17)).key == "st_patricks"  # today counts
    assert C.choose(s, date(2026, 11, 9)).key == "thanksgiving"
    assert C.choose(s, date(2027, 5, 1)).key == "memorial_day"
    assert C.choose(s, date(2027, 8, 1)).key == "labor_day"
    assert C.choose(s, date(2027, 10, 1)).key == "halloween"


def test_birthday_tomorrow_beats_a_holiday_next_week() -> None:
    s = _settings(birthday="12-19", birthday_name="Sam")
    ev = C.choose(s, date(2026, 12, 18))
    assert ev.key == "birthday" and ev.days == 1
    assert ev.label == "Sam's Birthday" and ev.celebration == "Happy Birthday, Sam!"
    assert C.choose(_settings(birthday="12-19"), date(2026, 12, 18)).label == "Your birthday"
    assert C.choose(_settings(birthday="12-19"), date(2026, 12, 19)).celebration == (
        "Happy Birthday!"
    )


def test_one_off_custom_date_takes_over_then_releases() -> None:
    s = _settings(custom_date="2027-06-12", custom_label="Our trip")
    ev = C.choose(s, date(2026, 11, 9))
    assert ev.key == "custom" and ev.label == "Our trip" and ev.days == 215 and ev.once
    assert C.choose(s, date(2027, 6, 12)).today
    assert C.choose(s, date(2027, 6, 13)).key == "independence_day"  # automatic resumes
    assert C.choose(_settings(custom_date="2027-06-12"), date(2027, 6, 1)).label == (
        C.DEFAULT_CUSTOM_LABEL
    )


def test_yearly_custom_date_competes_like_a_birthday() -> None:
    # decision: MM-DD never "passes", so it joins the automatic pool instead of taking over
    s = _settings(custom_date="11-10", custom_label="Anniversary")
    ev = C.choose(s, date(2026, 11, 9))
    assert ev.key == "custom" and ev.days == 1 and not ev.once
    assert C.choose(s, date(2026, 11, 11)).key == "thanksgiving"
    assert C.choose(s, date(2027, 11, 1)).key == "custom"


def test_holidays_filter_limits_the_automatic_pick() -> None:
    assert C.choose(_settings(holidays=["christmas"]), date(2026, 11, 9)).key == "christmas"
    assert C.choose(_settings(holidays=["Christmas ", "christmas"]), date(2026, 11, 9)).key == (
        "christmas"
    )
    problem = C.choose(_settings(holidays=[]), date(2026, 11, 9))
    assert isinstance(problem, C.Problem) and problem.field == "holidays"
    # a birthday alone is enough
    assert C.choose(_settings(holidays=[], birthday="01-02"), date(2026, 11, 9)).key == "birthday"


def test_pinned_event_ignores_the_filter_and_the_custom_date() -> None:
    s = _settings(event="halloween", holidays=["christmas"], custom_date="2027-01-01")
    assert C.choose(s, date(2026, 11, 9)).key == "halloween"


# --- settings and their problems -----------------------------------------------------------


def test_schema_defaults_and_options() -> None:
    s = _settings()
    assert s["event"] == "auto" and s["layout"] == "rotate"
    assert s["holidays"] == list(C.HOLIDAY_KEYS)
    assert s["birthday"] == s["birthday_name"] == s["custom_label"] == s["custom_date"] == ""
    event = next(f for f in C.SETTINGS_SCHEMA if f.key == "event")
    assert [v for v, _ in event.options] == ["auto", *C.EVENT_KEYS]
    assert dict(event.options)["st_patricks"] == "St. Patrick's Day"
    layout = next(f for f in C.SETTINGS_SCHEMA if f.key == "layout")
    assert [v for v, _ in layout.options] == ["rotate", *L.LAYOUT_NAMES]
    assert "mothers_day" not in C.EVENT_KEYS and "fathers_day" not in C.EVENT_KEYS


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"birthday": "13-01"}, "birthday: the month must be 01-12, got 13"),
        ({"birthday": "02-30"}, "birthday: February has 29 days, got 30"),
        ({"birthday": "04-31"}, "birthday: April has 30 days, got 31"),
        ({"birthday": "3/14"}, "birthday: use MM-DD, e.g. 03-14, got '3/14'"),
        ({"custom_date": "2027-02-29"}, "custom_date: 2027 has no 02-29"),
        ({"custom_date": "2027-13-01"}, "custom_date: the month must be 01-12, got 13"),
        ({"custom_date": "0000-01-01"}, "custom_date: the year must be 0001-9999, got 0"),
        ({"custom_date": "12 June"}, "custom_date: use YYYY-MM-DD or MM-DD, e.g. 2027-06-12"),
        ({"holidays": ["christmas", "xmas"]}, "holidays: unknown event 'xmas'; one of new_year"),
    ],
)
def test_validation_messages_name_the_field(settings: dict, message: str) -> None:
    with pytest.raises(ValueError) as e:
        C.validate(_settings(**settings))
    assert message in str(e.value)
    # the same problem is a card, never an error, at render time
    problem = C.choose(_settings(**settings), date(2026, 11, 9))
    assert isinstance(problem, C.Problem)
    assert problem.field == next(iter(settings))


def test_valid_settings_pass_validate() -> None:
    C.validate(_settings())
    C.validate(_settings(birthday="02-29", custom_date="12-31"))
    C.validate(_settings(custom_date="2027-06-12", holidays=["easter", "christmas"]))


def test_app_exposes_validate_for_the_settings_api() -> None:
    # PUT /api/apps/countdown/settings runs it after the schema check, so an
    # unknown holiday key is a 422 naming the field rather than a card
    assert C.APP.validate(_settings()) is None
    with pytest.raises(ValueError, match=r"^holidays: unknown event 'xmas'"):
        C.APP.validate(_settings(holidays=["xmas"]))


def test_pinned_events_without_a_date_ask_for_one() -> None:
    p = C.choose(_settings(event="birthday"), date(2026, 11, 9))
    assert isinstance(p, C.Problem) and p.field == "birthday"
    assert "Set the date on the Apps page" in p.line
    p = C.choose(_settings(event="custom"), date(2026, 11, 9))
    assert isinstance(p, C.Problem) and p.field == "custom_date"
    p = C.choose(_settings(event="custom", custom_label="Our trip"), date(2026, 11, 9))
    assert p.headline == "When is our trip?"
    p = C.choose(_settings(event="custom", custom_date="2026-01-01"), date(2026, 11, 9))
    assert isinstance(p, C.Problem) and "has passed" in p.headline


def test_problem_cards_render(tmp_path: Path) -> None:
    cases = [
        {"event": "birthday"},
        {"event": "custom"},
        {"birthday": "13-01"},
        {"custom_date": "2027-02-30"},
        {"holidays": ["xmas"]},
        {"holidays": []},
        {"event": "custom", "custom_date": "2026-01-01"},
    ]
    for settings in cases:
        frame = render_to_frame(C.APP, _ctx(tmp_path, settings=settings))
        assert frame.shape == (400, 600) and frame.dtype == np.uint8
        assert set(np.unique(frame)) <= {BLACK, WHITE, RED}
        assert (frame == BLACK).mean() > 0.01  # there is text on it


# --- motifs ---------------------------------------------------------------------------------


@pytest.mark.parametrize("key", C.EVENT_KEYS)
def test_every_motif_is_dithered_art_not_a_flat_fill(key: str) -> None:
    for w, h in ((280, 320), (600, 150), (200, 150)):
        m = A.render_motif(key, w, h)
        assert m.indices.shape == (h, w) and m.indices.dtype == np.uint8
        assert m.mask.shape == (h, w) and m.mask.dtype == bool
        assert m.indices.max() < N_COLORS
        covered = m.mask.mean()
        assert 0.08 < covered < 0.95, (key, w, h, covered)
        inside = m.indices[m.mask]
        assert len(np.unique(inside)) > 2, (key, w, h)
        # a dithered tone, not NOMINAL-only flat colour: the most common index
        # inside the drawing does not take the whole drawing
        counts = np.bincount(inside, minlength=N_COLORS)
        assert counts.max() < 0.9 * inside.size, (key, w, h)


def test_motifs_are_deterministic_and_distinct() -> None:
    a = A.render_motif("halloween", 280, 320)
    b = A.render_motif("halloween", 280, 320)
    assert np.array_equal(a.indices, b.indices) and np.array_equal(a.mask, b.mask)
    masks = {k: A.render_motif(k, 280, 320).mask for k in C.EVENT_KEYS}
    keys = list(masks)
    for i, k in enumerate(keys):
        for other in keys[i + 1 :]:
            assert (masks[k] != masks[other]).mean() > 0.05, (k, other)


def test_exact_palette_paint_stays_flat_after_dithering() -> None:
    scene = A.paint_motif("memorial_day", 280, 320)
    red = np.array(MEASURED_DEFAULT.rgb[RED], dtype=np.uint8)
    flat_red = np.all(scene.rgb_u8() == red, axis=-1)
    assert flat_red.sum() > 2000  # the stripes
    m = A.render_motif("memorial_day", 280, 320)
    assert (m.indices[flat_red] == RED).all()


def test_caption_boxes() -> None:
    assert A.render_motif("new_year", 280, 320).caption_box is not None
    labor = A.render_motif("labor_day", 600, 150)
    assert labor.caption_box is not None and labor.caption_fill == WHITE
    assert A.render_motif("christmas", 280, 320).caption_box is None
    with pytest.raises(ValueError, match="unknown motif"):
        A.paint_motif("mothers_day", 100, 100)


# --- the cards ----------------------------------------------------------------------------


def _number_is_crisp(frame: np.ndarray, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    region = frame[y0:y1, x0:x1]
    assert region.size > 2000, box
    assert set(np.unique(region)) <= {BLACK, WHITE}, "dither noise inside the number"
    assert (region == BLACK).mean() > 0.12, "the number is not solid"


@pytest.mark.parametrize("variant", range(3))
@pytest.mark.parametrize("key", C.EVENT_KEYS)
def test_every_event_renders_in_every_layout(tmp_path: Path, key: str, variant: int) -> None:
    ctx = _ctx(tmp_path, settings=_pin(key), variant=variant)
    event = C.choose(ctx.settings, C.local_today(ctx))
    assert isinstance(event, C.Event) and event.key == key and not event.today
    frame, box = L.compose(FONTS, event, variant, C.local_today(ctx))
    assert frame.shape == (400, 600) and frame.dtype == np.uint8 and frame.max() < N_COLORS
    assert box is not None
    _number_is_crisp(frame, box)
    # the motif made it onto the card: more than black and white, in its box
    mx, my, mw, mh = (L.POSTER_MOTIF, L.CALENDAR_MOTIF, L.BANNER_MOTIF)[variant]
    motif_region = frame[my : my + mh, mx : mx + mw]
    assert len(np.unique(motif_region)) > 2
    assert np.array_equal(frame, render_to_frame(C.APP, ctx))


@pytest.mark.parametrize("variant", range(3))
def test_day_of_and_three_digit_counts(tmp_path: Path, variant: int) -> None:
    xmas = datetime(2026, 12, 25, 9, 0, tzinfo=EST)
    ctx = _ctx(tmp_path, xmas, settings={"event": "christmas"}, variant=variant)
    event = C.choose(ctx.settings, xmas.date())
    frame, box = L.compose(FONTS, event, variant, xmas.date())
    assert frame.shape == (400, 600)
    if variant == L.CALENDAR:
        _number_is_crisp(frame, box)  # the date stays on the page
    else:
        assert box is None  # the celebration replaces the number
    assert (frame == BLACK).mean() > 0.02
    later = datetime(2027, 1, 2, 9, 0, tzinfo=EST)
    event = C.choose(_settings(event="christmas"), later.date())
    assert event.days == 357
    frame, box = L.compose(FONTS, event, variant, later.date())
    assert box is not None
    _number_is_crisp(frame, box)
    if variant != L.CALENDAR:
        assert box[2] - box[0] > 150  # three digits wide


@pytest.mark.parametrize("variant", range(3))
def test_long_names_fit_every_layout(tmp_path: Path, variant: int) -> None:
    cases = [
        {"event": "st_patricks"},
        {"event": "custom", "custom_date": "2027-04-03", "custom_label": "A" * 40},
        {"event": "custom", "custom_date": "2027-04-03", "custom_label": "Our trip to " * 3},
        {"event": "birthday", "birthday": "11-22", "birthday_name": "Alexandra-Josephine Montgo"},
        {"event": "birthday", "birthday": "11-22", "birthday_name": "W" * 30},
        # beyond the brief's limits: one unbreakable 33-character word, a 500-character name
        {"event": "birthday", "birthday": "11-22", "birthday_name": LONG_WORD_NAME},
        {"event": "birthday", "birthday": "11-22", "birthday_name": "Maximilian " * 45},
        {"event": "birthday", "birthday": "11-09", "birthday_name": "Maximilian " * 45},
    ]
    for settings in cases:
        frame = render_to_frame(C.APP, _ctx(tmp_path, settings=settings, variant=variant))
        assert frame.shape == (400, 600) and frame.max() < N_COLORS
        # nothing bleeds into the outer 4 px of the card (type and the motif stay inside)
        edge = np.concatenate([frame[:4].ravel(), frame[-4:].ravel(), frame[:, :4].ravel()])
        assert variant == L.BANNER or (edge == WHITE).all()


LONG_WORD_NAME = "Alexandria Montgomery-Fitzwilliam"


def _whole_words(lines: list[str], text: str) -> None:
    """Every line but the last is made of whole words of ``text``; the last ends
    in an ellipsis exactly when the text was cut."""
    words = set(text.split())
    for line in lines[:-1]:
        assert line.split() and set(line.split()) <= words, lines
    last = lines[-1]
    assert last.endswith("…") == (" ".join(lines) != text), lines


@pytest.mark.parametrize(
    ("make", "max_size", "min_size", "max_lines", "height"),
    [
        (lambda s: FONTS.font("sans", s, "semibold"), 30, 16, 2, 64),  # the poster's label
        (lambda s: FONTS.font("serif", s, "bold"), 40, 16, 3, 132),  # the calendar's name
    ],
)
def test_fit_block_never_drops_words_or_splits_them(make, max_size, min_size, max_lines, height):
    c = L.Canvas()
    span = 252
    label = f"{LONG_WORD_NAME}'s Birthday"
    # fit_text alone needs more lines than the layout draws (that was the bug)
    _f, raw, _s = c.fit_text(label, span, height, make, max_size=max_size, min_size=min_size)
    assert len(raw) > max_lines or any(c.text_width(w, _f) > span for w in label.split())
    font, lines = L.fit_block(
        c, label, span, height, make, max_size=max_size, min_size=min_size, max_lines=max_lines
    )
    assert 1 <= len(lines) <= max_lines, lines
    assert all(c.text_width(line, font) <= span for line in lines)
    assert font.size >= min_size
    _whole_words(lines, label)
    # a word wider than the column at the smallest size is the one place a cut may fall
    # inside a word, and then it is ellipsized rather than wrapped onto the next line
    too_wide = c.text_width("Montgomery-Fitzwilliam's", font) > span
    assert too_wide or "Montgomery-Fitzwilliam's" in " ".join(lines), lines
    # the brief's longest names still come through untouched and at a readable size
    for name in (
        "Alexandra-Josephine Montgo's Birthday",
        "Christopher Featherstonehaugh's Birthday",
    ):
        f, ls = L.fit_block(
            c, name, span, height, make, max_size=max_size, min_size=min_size, max_lines=max_lines
        )
        assert " ".join(ls) == name and f.size >= min_size, ls
    # thirty W's plus "'s Birthday" is one word wider than the column even at min_size, so
    # it has to break mid-word: with lines to spare it is shown whole, otherwise the tail is
    # cut with a visible "…" (the old poster wrapped it mid-word and dropped "Birthday")
    wide = "W" * 30 + "'s Birthday"
    f, ls = L.fit_block(
        c, wide, span, height, make, max_size=max_size, min_size=min_size, max_lines=max_lines
    )
    assert len(ls) <= max_lines and f.size == min_size, ls
    assert "".join(ls).replace(" ", "") == wide.replace(" ", "") or ls[-1].endswith("…"), ls
    # no max_lines: as many as fit the height, and a 500-character name is cut with "…"
    long = "Maximilian " * 45 + "Rex"
    f, ls = L.fit_block(c, long, span, height, make, max_size=max_size, min_size=min_size)
    assert len(ls) * c.line_height(f, 1.1) <= height and ls[-1].endswith("…"), ls


def test_render_is_deterministic_and_a_protocol_round_trip(tmp_path: Path) -> None:
    assert isinstance(C.APP, App)
    assert C.APP.id == "countdown" and C.APP.variants == 3 and C.APP.needs_dither is True
    ctx = _ctx(tmp_path, variant=0)
    a = render_to_frame(C.APP, ctx)
    b = render_to_frame(C.APP, _ctx(tmp_path, variant=0))
    assert np.array_equal(a, b)
    # the app returns indices: render_to_frame passes them through without a dither pass
    raw = C.APP.render(ctx)
    assert isinstance(raw, np.ndarray) and np.array_equal(raw, a)
    # dithering against another palette changes the motif, never the type
    event = C.choose(ctx.settings, NOW.date())
    measured, box = L.compose(FONTS, event, 0, NOW.date(), palette=MEASURED_DEFAULT)
    nominal, box2 = L.compose(FONTS, event, 0, NOW.date(), palette=NOMINAL)
    assert box == box2 and np.array_equal(measured, a)
    x0, y0, x1, y1 = box
    assert np.array_equal(measured[y0:y1, x0:x1], nominal[y0:y1, x0:x1])
    mx, my, mw, mh = L.POSTER_MOTIF
    assert not np.array_equal(
        measured[my : my + mh, mx : mx + mw], nominal[my : my + mh, mx : mx + mw]
    )


def test_pick_variant_order(tmp_path: Path) -> None:
    s = _settings()
    assert C.pick_variant(_ctx(tmp_path, variant=2), s) == 2
    assert C.pick_variant(_ctx(tmp_path, variant=4), s) == 1
    assert C.pick_variant(_ctx(tmp_path), _settings(layout="banner")) == 2
    assert C.pick_variant(_ctx(tmp_path, seed=7), s) == 1
    assert C.pick_variant(_ctx(tmp_path, variant=0, seed=7), _settings(layout="banner")) == 0
    for hour in range(24):
        now = NOW.replace(hour=hour)
        assert C.pick_variant(_ctx(tmp_path, now), s) == hour % 3


def test_describe_lines() -> None:
    assert C.describe(NOW, _settings()) == "Thanksgiving in 17 days (2026-11-26)"
    assert C.describe(NOW, _settings(event="birthday")).startswith("problem:")
