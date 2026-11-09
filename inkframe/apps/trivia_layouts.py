"""The three Trivia cards, drawn in palette colours with ``Canvas``.

0. **poster**: a "DID YOU KNOW?" eyebrow in tracked Inter capitals with a
   coloured rule (the colour follows the category), the fact set large in
   Playfair Display with balanced lines, the category bottom left and the
   source bottom right in mono.  With an "on this day" event the eyebrow
   reads "ON THIS DAY IN 1985" and the licence line replaces the source.
1. **onthisday**: "ON THIS DAY · NOVEMBER 9" over a red rule, then two or
   three dated events (the year in bold mono, the text in Inter) and the
   licence line.  With bundled data the header says "TODAY'S FACT" and the
   fact is set like the poster's.
2. **index**: an index card: a coloured tab with the category, a double red
   top rule, blue ruled lines with the fact written on them, the source in
   mono bottom right.

Facts with digits are set in Inter (lining figures); Playfair Display's
figures are old-style and read oddly in a date or a measurement.
``show_source`` hides the bundled source note; the Wikipedia licence line is
required and stays whatever the setting.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from PIL import ImageFont

from inkframe.apps.base import FontLibrary
from inkframe.apps.draw import Canvas
from inkframe.apps.quotes_type import (
    SERIF_THRESHOLD,
    draw_tracked,
    fit_label,
    fit_quote,
    line_width,
    overflows,
    tracked_width,
    vmetrics,
    wrap_measure,
)
from inkframe.apps.trivia_data import LICENCE, Event, Fact
from inkframe.display.palette import BLACK, BLUE, GREEN, RED, WHITE, YELLOW

VARIANT_NAMES: tuple[str, ...] = ("poster", "onthisday", "index")
POSTER, ONTHISDAY, INDEX = range(3)

W, H = 600, 400
MARGIN = 40
MEASURE = W - 2 * MARGIN
#: the one-line footer: category / date left, source / licence right
FOOTER_BASELINE = 382
#: the index card's ruled lines
RULE_TOP, RULE_STEP, RULE_COUNT = 118, 36, 8
#: text sits this far above its ruled line
RULE_LIFT = 8

#: the rule, tab and accent colour per category
_CATEGORY_COLOUR = {
    "science": BLUE,
    "space": BLUE,
    "nature": GREEN,
    "animals": GREEN,
    "history": RED,
    "geography": RED,
    "language": YELLOW,
    "numbers": YELLOW,
    "food": YELLOW,
    "on this day": RED,
}

MakeFont = Callable[[int], ImageFont.FreeTypeFont]


def category_colour(category: str) -> int:
    return _CATEGORY_COLOUR.get(category, BLACK)


def month_day(now: datetime) -> str:
    """``November 9`` (no leading zero, whatever the platform)."""
    return f"{now:%B} {now.day}"


def body_font(fonts: FontLibrary, text: str) -> tuple[MakeFont, int]:
    """``(make_font, threshold)`` for the fact body: Playfair unless the text
    carries digits, which Inter sets with lining figures."""
    if any(ch.isdigit() for ch in text):
        return (lambda s: fonts.font("sans", s, "regular")), 128
    return (lambda s: fonts.font("serif", s, "regular")), SERIF_THRESHOLD


def _eyebrow(c: Canvas, fonts: FontLibrary, text: str, rule: int | None) -> int:
    """Tracked capitals top left, a rule running from them to the right
    margin; returns the y below the eyebrow."""
    baseline = 60
    f, t, text = fit_label(fonts, text, 360, "sans", "semibold", 17, 3.0, min_size=14)
    box = draw_tracked(c, MARGIN, baseline, text, f, BLACK, tracking=t)
    if rule is not None:
        cap = vmetrics(f)[0]
        ry = baseline - cap // 2 - 2
        x0 = box[2] + 18
        if x0 < W - MARGIN - 40:
            c.rect((x0, ry, W - MARGIN - 1, ry + 3), fill=rule)
    return baseline + 10


def _footer(
    c: Canvas,
    fonts: FontLibrary,
    left: str,
    right: str,
    *,
    baseline: int = FOOTER_BASELINE,
) -> None:
    """One line at the bottom: a tracked label left, a mono note right."""
    right_edge = W - MARGIN
    if right:
        f, t, text = fit_label(fonts, right, 300, "mono", "regular", 14, 0.0)
        box = draw_tracked(c, right_edge, baseline, text, f, BLACK, tracking=t, align="right")
        right_edge = box[0] - 24
    if left:
        f, t, text = fit_label(
            fonts, left.upper(), right_edge - MARGIN, "sans", "medium", 14, 2.5, min_size=14
        )
        draw_tracked(c, MARGIN, baseline, text, f, BLACK, tracking=t)


def _fact_block(
    c: Canvas, fonts: FontLibrary, text: str, top: int, bottom: int, *, max_size: int = 56
) -> None:
    """The fact set as large as fits between ``top`` and ``bottom``, left
    aligned, sitting a little above the middle of its space."""
    make_font, threshold = body_font(fonts, text)
    fit = fit_quote(text, make_font, MEASURE, bottom - top, max_size=max_size, leading=1.24)
    y = top + int(max(0, bottom - top - fit.height) * 0.42)
    for i, line in enumerate(fit.lines):
        c.text(
            (MARGIN, y + fit.ascent + i * fit.leading),
            line,
            fit.font,
            BLACK,
            anchor="ls",
            threshold=threshold,
        )


def _fact_footer(fonts: FontLibrary, c: Canvas, fact: Fact, show_source: bool, now: datetime):
    if fact.origin == "onthisday":
        _footer(c, fonts, month_day(now), LICENCE)
    else:
        _footer(c, fonts, fact.category, fact.source if show_source else "")


# --- 0: poster ---------------------------------------------------------------------------


def poster(c: Canvas, fonts: FontLibrary, fact: Fact, events, show_source: bool, now: datetime):
    if fact.origin == "onthisday" and fact.year is not None:
        eyebrow = f"ON THIS DAY IN {fact.year}"
    else:
        eyebrow = "DID YOU KNOW?"
    top = _eyebrow(c, fonts, eyebrow, category_colour(fact.category)) + 22
    _fact_block(c, fonts, fact.text, top, FOOTER_BASELINE - 14 - 28)
    _fact_footer(fonts, c, fact, show_source, now)


# --- 1: on this day ----------------------------------------------------------------------


def _header(c: Canvas, fonts: FontLibrary, text: str) -> int:
    baseline = 58
    f, t, text = fit_label(fonts, text, MEASURE, "sans", "semibold", 20, 3.0, min_size=15)
    draw_tracked(c, MARGIN, baseline, text, f, BLACK, tracking=t)
    c.rect((MARGIN, 74, W - MARGIN - 1, 76), fill=RED)
    return 98


def _lay_events(
    fonts: FontLibrary, events: list[Event], width: int, height: int
) -> tuple[int, int, int, list[list[str]]] | None:
    """``(size, leading, year column width, wrapped lines per event)`` at the
    largest size from 24 px down at which ``events`` fit ``height``; ``None``
    when even 16 px will not do."""
    gap = 14
    for size in range(26, 15, -1):
        text_f = fonts.font("sans", size, "regular")
        year_f = fonts.font("mono", size, "bold")
        leading = int(round(size * 1.3))
        col = int(line_width(year_f, "8888")) + 18

        def measure(_i: int, width: int = width - col) -> int:
            return width

        wrapped = [wrap_measure(e.text, text_f, measure) for e in events]
        if any(overflows(lines, text_f, measure) for lines in wrapped):
            continue
        a, d = vmetrics(text_f)
        total = sum(a + (len(lines) - 1) * leading + d for lines in wrapped)
        total += gap * (len(events) - 1)
        if total <= height:
            return size, leading, col, wrapped
    return None


def onthisday(c: Canvas, fonts: FontLibrary, fact: Fact, events, show_source: bool, now: datetime):
    date = month_day(now).upper()
    if fact.origin != "onthisday" or not events:
        top = _header(c, fonts, f"TODAY’S FACT · {date}")
        _fact_block(c, fonts, fact.text, top + 8, FOOTER_BASELINE - 14 - 28, max_size=50)
        _fact_footer(fonts, c, fact, show_source, now)
        return
    top = _header(c, fonts, f"ON THIS DAY · {date}")
    bottom = FOOTER_BASELINE - 14 - 20
    shown = list(events)
    laid = None
    while shown:
        laid = _lay_events(fonts, shown, MEASURE, bottom - top)
        if laid is not None:
            break
        shown = shown[:-1]
    if laid is None:  # pragma: no cover - a single event of 220 chars fits at 16 px
        shown, laid = [events[0]], _lay_events(fonts, [events[0]], MEASURE, bottom - top + 40)
    size, leading, col, wrapped = laid
    text_f = fonts.font("sans", size, "regular")
    year_f = fonts.font("mono", size, "bold")
    a, d = vmetrics(text_f)
    y = top
    for event, lines in zip(shown, wrapped, strict=True):
        baseline = y + a
        c.text((MARGIN, baseline), str(event.year), year_f, RED, anchor="ls")
        for i, line in enumerate(lines):
            c.text((MARGIN + col, baseline + i * leading), line, text_f, BLACK, anchor="ls")
        y = baseline + (len(lines) - 1) * leading + d + 14
    _footer(c, fonts, "", LICENCE)


# --- 2: index card -----------------------------------------------------------------------


def _tab(c: Canvas, fonts: FontLibrary, label: str, colour: int) -> None:
    f, t, text = fit_label(fonts, label.upper(), 300, "sans", "semibold", 15, 2.5, min_size=14)
    width = int(tracked_width(f, text, t)) + 2 * 18
    x0, y0, x1, y1 = MARGIN, 24, MARGIN + width, 70
    c.rect((x0, y0, x1, y1), fill=colour, radius=9)
    c.rect((x0, y0 + 14, x1, y1), fill=colour)  # square bottom corners
    ink = BLACK if colour in (YELLOW, WHITE) else WHITE
    cap = vmetrics(f)[0]
    draw_tracked(
        c, (x0 + x1) / 2, (y0 + y1) // 2 + cap // 2, text, f, ink, tracking=t, align="center"
    )


def _rules(c: Canvas) -> list[int]:
    c.rect((24, 70, W - 25, 72), fill=RED)
    c.rect((24, 77, W - 25, 77), fill=RED)
    ys = [RULE_TOP + k * RULE_STEP for k in range(RULE_COUNT)]
    for y in ys:
        c.rect((24, y, W - 25, y), fill=BLUE)
    return ys


def _fit_ruled(
    text: str,
    make_font: MakeFont,
    year_f_at: Callable[[int], ImageFont.FreeTypeFont],
    year: str,
    measure: int,
    rows: int,
    sizes: range,
) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont, int, list[str]] | None:
    """The largest size in ``sizes`` at which ``text`` (after the year on the
    first row) fits ``rows`` ruled rows; ``None`` if none does."""
    for size in sizes:
        f = make_font(size)
        year_f = year_f_at(size)
        indent = int(line_width(year_f, year)) + 16 if year else 0

        def width_at(i: int, indent: int = indent) -> int:
            return measure - indent if i == 0 else measure

        lines = wrap_measure(text, f, width_at)
        if len(lines) <= rows and not overflows(lines, f, width_at):
            return f, year_f, indent, lines
    return None


def index(c: Canvas, fonts: FontLibrary, fact: Fact, events, show_source: bool, now: datetime):
    colour = category_colour(fact.category)
    _tab(c, fonts, fact.category, colour)
    rules = _rules(c)
    footer = fact.origin == "onthisday" or (show_source and bool(fact.source))
    usable = rules[:-1] if footer else rules
    left = MARGIN + 8
    measure = W - MARGIN - left
    make_font, threshold = body_font(fonts, fact.text)
    year = str(fact.year) if fact.origin == "onthisday" and fact.year is not None else ""
    # a short fact is written large on every other line (from the second, clear
    # of the top rule); a long one fills the lines
    every_other = usable[1::2]
    chosen = _fit_ruled(
        fact.text,
        make_font,
        lambda s: fonts.font("mono", s - 6, "bold"),
        year,
        measure,
        len(every_other),
        range(44, 33, -1),
    )
    rows = every_other
    if chosen is None:
        rows = usable
        chosen = _fit_ruled(
            fact.text,
            make_font,
            lambda s: fonts.font("mono", s - 2, "bold"),
            year,
            measure,
            len(usable),
            range(26, 17, -1),
        )
    if chosen is None:  # 180 characters fit at 18 px; be safe anyway
        f = make_font(18)
        year_f = fonts.font("mono", 16, "bold")
        indent = int(line_width(year_f, year)) + 16 if year else 0
        lines = wrap_measure(fact.text, f, lambda i: measure - indent if i == 0 else measure)
        lines = lines[: len(usable)]
        if len(lines) == len(usable):
            lines[-1] = lines[-1].rstrip(",;:—") + "…"
        chosen = (f, year_f, indent, lines)
    f, year_f, indent, lines = chosen
    for i, line in enumerate(lines):
        baseline = rows[i] - RULE_LIFT
        if i == 0 and year:
            c.text((left, baseline), year, year_f, RED, anchor="ls")
        x = left + (indent if i == 0 else 0)
        c.text((x, baseline), line, f, BLACK, anchor="ls", threshold=threshold)
    if fact.origin == "onthisday":
        _footer(c, fonts, "", LICENCE, baseline=rules[-1] - RULE_LIFT)
    elif footer:
        _footer(c, fonts, "", f"Source: {fact.source}", baseline=rules[-1] - RULE_LIFT)


# --- the placeholder -------------------------------------------------------------------


def placeholder(c: Canvas, fonts: FontLibrary, title: str, detail: str) -> None:
    """A tidy card for "nothing to show" states (empty pool, missing library)."""
    top = _eyebrow(c, fonts, "TRIVIA", BLACK) + 22
    _fact_block(c, fonts, title, top, FOOTER_BASELINE - 14 - 28, max_size=44)
    f, t, detail = fit_label(fonts, detail, MEASURE, "mono", "regular", 14, 0.0)
    draw_tracked(c, MARGIN, FOOTER_BASELINE, detail, f, BLACK, tracking=t)


_LAYOUTS = (poster, onthisday, index)


def compose(
    fonts: FontLibrary,
    fact: Fact,
    variant: int,
    now: datetime,
    *,
    events: list[Event] | None = None,
    show_source: bool = True,
) -> Canvas:
    """Draw one card.  Pure: the same inputs give the same pixels."""
    c = Canvas(background=WHITE)
    _LAYOUTS[int(variant) % len(_LAYOUTS)](c, fonts, fact, events or [], show_source, now)
    return c
