"""The three Countdown layouts and the settings-problem card.

Every layout pastes the dithered motif (``countdown_art.render_motif``)
through its coverage mask first and sets the type afterwards in flat palette
colours with ``Canvas``, so the number and the labels are never dithered.

0. **poster**: the motif large on the left, "DAYS UNTIL" over a very large
   number (Playfair Display Bold, up to 160 px) and the event's name on the
   right; on the day the number gives way to the celebration line.
1. **calendar**: a tear-off calendar page (a red header band with the month,
   the date very large, the weekday under it), the event's name and the count
   as a caption on the right, the motif small in the bottom corner.
2. **banner**: the motif across the top, the count and the name centred
   below in Inter.

Names and celebration lines go through ``fit_block``: the type shrinks before
a word is split, and a name too long even at the smallest size is folded into
the last line and ellipsized, never silently cut.

``compose`` returns the frame and the box of the largest numeral on the card
(the count, or the calendar's date), which the tests use to check that the
number is solid black on white.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from inkframe.apps.base import FontLibrary
from inkframe.apps.countdown_art import Motif, render_motif
from inkframe.apps.draw import Canvas, rgb
from inkframe.apps.quotes_type import draw_tracked, fit_label
from inkframe.display.palette import (
    BLACK,
    MEASURED_DEFAULT,
    NOMINAL,
    RED,
    WHITE,
    Palette,
    indices_to_image,
)

if TYPE_CHECKING:  # pragma: no cover
    from inkframe.apps.countdown import Event, Problem

LAYOUT_NAMES: tuple[str, ...] = ("poster", "calendar", "banner")
POSTER, CALENDAR, BANNER = range(3)

W, H = 600, 400
M = 32
Box = tuple[int, int, int, int]

#: motif boxes per layout, ``(x, y, width, height)``
POSTER_MOTIF = (24, 40, 280, 320)
CALENDAR_MOTIF = (372, 222, 200, 150)
BANNER_MOTIF = (0, 0, 600, 150)

#: the crisp lettering a motif asks for (``Motif.caption_box``)
LABOR_DAY_CAPTION = "LABOR DAY"


# --- helpers -----------------------------------------------------------------------------------


def fit_line(
    make_font: Callable[[int], ImageFont.FreeTypeFont],
    text: str,
    max_width: int,
    max_size: int,
    min_size: int,
) -> ImageFont.FreeTypeFont:
    """The largest size at which one line of ``text`` fits ``max_width``."""
    lo, hi = min_size, max_size
    best = make_font(min_size)
    while lo <= hi:
        mid = (lo + hi) // 2
        f = make_font(mid)
        if f.getlength(text) <= max_width:
            best, lo = f, mid + 1
        else:
            hi = mid - 1
    return best


def ellipsize(c: Canvas, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    if c.text_width(text, font) <= max_width:
        return text
    while text and c.text_width(text + "…", font) > max_width:
        text = text[:-1].rstrip()
    return text + "…"


def fit_block(
    c: Canvas,
    text: str,
    max_width: int,
    max_height: int,
    make_font: Callable[[int], ImageFont.FreeTypeFont],
    *,
    max_size: int,
    min_size: int,
    spacing: float = 1.1,
    max_lines: int | None = None,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """``Canvas.fit_text`` for a block that must never lose words silently.

    The size shrinks (down to ``min_size``) while a word would be split across
    lines or the text needs more than ``max_lines`` (default: as many as fit
    ``max_height``).  Whatever still does not fit is folded into the last line
    and ellipsized, so the card shows a visible "…" rather than dropping the
    tail of a 500-character name or breaking it mid-word.
    """
    font, lines, size = c.fit_text(
        text,
        max_width,
        max_height,
        make_font,
        max_size=max_size,
        min_size=min_size,
        spacing=spacing,
    )
    words = text.split()

    def splits_a_word(f: ImageFont.FreeTypeFont) -> bool:
        return any(c.text_width(w, f) > max_width for w in words)

    def allowed(f: ImageFont.FreeTypeFont) -> int:
        if max_lines is not None:
            return max_lines
        return max(1, int(max_height // c.line_height(f, spacing)))

    while size > min_size and (len(lines) > allowed(font) or splits_a_word(font)):
        size -= 1
        font = make_font(size)
        lines = c.wrap(text, font, max_width)
    keep = allowed(font)
    if len(lines) > keep:
        lines = lines[: keep - 1] + [" ".join(lines[keep - 1 :])]
    return font, [ellipsize(c, line, font, max_width) for line in lines]


def halo_text(
    c: Canvas,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: int,
    halo: int,
    *,
    anchor: str = "mm",
    width: int = 4,
) -> Box:
    """Crisp text with a solid ``halo`` outline, so it reads over a drawing."""
    mask = Image.new("L", c.image.size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=font, fill=255, anchor=anchor, stroke_width=width)
    mask = mask.point(lambda v: 255 if v >= 128 else 0)
    c.image.paste(rgb(halo), (0, 0, c.width, c.height), mask)
    return c.text(xy, text, font, fill, anchor=anchor)


def paste_motif(c: Canvas, motif: Motif, xy: tuple[int, int]) -> None:
    """Paste the dithered indices (as NOMINAL colours) through the coverage mask."""
    image = indices_to_image(motif.indices, NOMINAL)
    mask = Image.fromarray((motif.mask.astype(np.uint8) * 255), "L")
    c.paste(image, xy, mask)


def letter_caption(c: Canvas, fonts: FontLibrary, motif: Motif, xy: tuple[int, int], event: Event):
    """Set the caption a motif leaves room for: the coming year for New Year's
    Day, the ribbon's words for Labor Day."""
    if motif.caption_box is None:
        return
    x0, y0, x1, y1 = motif.caption_box
    x0, x1, y0, y1 = x0 + xy[0], x1 + xy[0], y0 + xy[1], y1 + xy[1]
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    if event.key == "new_year":
        text = str(event.date.year)
        size_cap = min(int((y1 - y0) * 1.1), 150)
        font = fit_line(lambda s: fonts.font("serif", s, "bold"), text, x1 - x0, size_cap, 18)
        halo_text(c, (cx, cy), text, font, motif.caption_fill, WHITE, anchor="mm", width=5)
    elif event.key == "labor_day":
        f, t, text = fit_label(
            fonts, LABOR_DAY_CAPTION, x1 - x0, "sans", "bold", min(y1 - y0, 40), 4.0, 12
        )
        asc, desc = f.getmetrics()
        baseline = cy + (asc - desc) // 2
        draw_tracked(c, cx, baseline, text, f, motif.caption_fill, tracking=t, align="center")


def date_line(event: Event, today: date) -> str:
    d = event.date
    text = f"{d:%A}, {d:%B} {d.day}"
    return text if d.year == today.year else f"{text}, {d.year}"


def days_caption(days: int) -> str:
    return "1 day to go" if days == 1 else f"{days} days to go"


# --- the layouts ------------------------------------------------------------------------------


def _poster(c: Canvas, fonts: FontLibrary, event: Event, today: date, motif: Motif) -> Box | None:
    mx, my, mw, mh = POSTER_MOTIF
    paste_motif(c, motif, (mx, my))
    letter_caption(c, fonts, motif, (mx, my), event)
    x0, x1 = 324, W - 24
    span = x1 - x0
    cx = (x0 + x1) / 2
    eyebrow = "TODAY" if event.today else ("DAY UNTIL" if event.days == 1 else "DAYS UNTIL")
    f_eye, t_eye, eyebrow = fit_label(fonts, eyebrow, span, "sans", "semibold", 18, 3.5, 14)
    draw_tracked(c, cx, 112, eyebrow, f_eye, BLACK, tracking=t_eye, align="center")
    c.rect((int(cx) - 20, 124, int(cx) + 19, 126), fill=RED)
    number_box: Box | None = None
    if event.today:
        font, lines = fit_block(
            c,
            event.celebration,
            span,
            190,
            lambda s: fonts.font("serif", s, "bold"),
            max_size=60,
            min_size=24,
        )
        lh = c.line_height(font, 1.1)
        y = 150 + (190 - lh * len(lines)) // 2
        for line in lines:
            c.text((cx, y), line, font, BLACK, anchor="ma")
            y += lh
    else:
        text = str(event.days)
        font = fit_line(lambda s: fonts.font("serif", s, "bold"), text, span, 160, 72)
        number_box = c.text((cx, 268), text, font, BLACK, anchor="ms")
        font, lines = fit_block(
            c,
            event.label,
            span,
            64,
            lambda s: fonts.font("sans", s, "semibold"),
            max_size=30,
            min_size=16,
            max_lines=2,
        )
        lh = c.line_height(font, 1.1)
        y = 288
        for line in lines:
            c.text((cx, y), line, font, BLACK, anchor="ma")
            y += lh
    f_date = fonts.font("mono", 14)
    c.text((cx, 380), ellipsize(c, date_line(event, today), f_date, span), f_date, BLACK, "ms")
    return number_box


def _calendar(c: Canvas, fonts: FontLibrary, event: Event, today: date, motif: Motif) -> Box | None:
    # the page: a drop shadow, the sheet, two rings and a red header band
    px0, py0, px1, py1 = 40, 40, 300, 360
    c.rect((px0 + 6, py0 + 6, px1 + 6, py1 + 6), fill=BLACK)
    c.rect((px0, py0, px1, py1), fill=WHITE, outline=BLACK, width=2)
    head_h = 58
    c.rect((px0 + 2, py0 + 2, px1 - 2, py0 + head_h), fill=RED)
    for rx in (px0 + 60, px1 - 60):
        c.rect((rx - 7, py0 - 14, rx + 7, py0 + 18), fill=WHITE, outline=BLACK, width=2, radius=6)
        c.rect((rx - 3, py0 - 10, rx + 3, py0 + 12), fill=BLACK, radius=2)
    d = event.date
    header = f"{d:%B} {d.year}".upper()
    f_head, t_head, header = fit_label(fonts, header, px1 - px0 - 40, "sans", "bold", 22, 3.0, 14)
    draw_tracked(
        c, (px0 + px1) / 2, py0 + 42, header, f_head, WHITE, tracking=t_head, align="center"
    )
    text = str(d.day)
    font = fit_line(lambda s: fonts.font("serif", s, "bold"), text, px1 - px0 - 40, 150, 72)
    number_box = c.text(((px0 + px1) / 2, 262), text, font, BLACK, anchor="ms")
    f_day = fonts.font("sans", 22, "medium")
    c.text(((px0 + px1) / 2, 306), f"{d:%A}", f_day, BLACK, anchor="ms")
    if event.today:
        f_today, t_today, label = fit_label(
            fonts, "TODAY", px1 - px0 - 40, "sans", "semibold", 16, 4.0
        )
        draw_tracked(c, (px0 + px1) / 2, 338, label, f_today, RED, tracking=t_today, align="center")

    # the right column: the name, the count, the motif in the corner
    x0, x1 = 324, W - 28
    span = x1 - x0
    font, lines = fit_block(
        c,
        event.celebration if event.today else event.label,
        span,
        132,
        lambda s: fonts.font("serif", s, "bold"),
        max_size=40,
        min_size=16,
        max_lines=3,
    )
    lh = c.line_height(font, 1.1)
    y = 48
    for line in lines:
        c.text((x0, y), line, font, BLACK, anchor="la")
        y += lh
    caption = "Enjoy the day" if event.today else days_caption(event.days)
    f_cap = fit_line(lambda s: fonts.font("sans", s, "semibold"), caption, span, 26, 16)
    c.text((x0, max(y + 10, 186) + 20), caption, f_cap, BLACK, anchor="ls")
    mx, my, _mw, _mh = CALENDAR_MOTIF
    paste_motif(c, motif, (mx, my))
    letter_caption(c, fonts, motif, (mx, my), event)
    return number_box


def _banner(c: Canvas, fonts: FontLibrary, event: Event, today: date, motif: Motif) -> Box | None:
    mx, my, _mw, mh = BANNER_MOTIF
    paste_motif(c, motif, (mx, my))
    letter_caption(c, fonts, motif, (mx, my), event)
    c.rect((M, my + mh + 8, W - M - 1, my + mh + 9), fill=BLACK)
    cx = W / 2
    span = W - 2 * M
    number_box: Box | None = None
    if event.today:
        font, lines = fit_block(
            c,
            event.celebration,
            span,
            150,
            lambda s: fonts.font("sans", s, "bold"),
            max_size=64,
            min_size=24,
        )
        lh = c.line_height(font, 1.1)
        y = 186 + (150 - lh * len(lines)) // 2
        for line in lines:
            c.text((cx, y), line, font, BLACK, anchor="ma")
            y += lh
        f_date = fonts.font("sans", 20, "medium")
        c.text((cx, 372), date_line(event, today), f_date, BLACK, anchor="ms")
    else:
        eyebrow = "DAY UNTIL" if event.days == 1 else "DAYS UNTIL"
        f_eye, t_eye, eyebrow = fit_label(fonts, eyebrow, span, "sans", "semibold", 16, 4.0, 14)
        draw_tracked(c, cx, 194, eyebrow, f_eye, BLACK, tracking=t_eye, align="center")
        text = str(event.days)
        font = fit_line(lambda s: fonts.font("sans", s, "bold"), text, 400, 124, 64)
        number_box = c.text((cx, 318), text, font, BLACK, anchor="ms")
        f_name = fit_line(lambda s: fonts.font("sans", s, "semibold"), event.label, span, 30, 16)
        c.text((cx, 368), ellipsize(c, event.label, f_name, span), f_name, BLACK, anchor="ms")
    return number_box


_LAYOUTS = (_poster, _calendar, _banner)
_MOTIF_BOXES = (POSTER_MOTIF, CALENDAR_MOTIF, BANNER_MOTIF)


def compose(
    fonts: FontLibrary,
    event: Event,
    variant: int,
    today: date,
    *,
    palette: Palette = MEASURED_DEFAULT,
) -> tuple[np.ndarray, Box | None]:
    """Draw one card; returns the frame and the box of its largest numeral
    (``None`` when the card celebrates instead).  Pure for a given input."""
    variant = int(variant) % len(_LAYOUTS)
    _x, _y, mw, mh = _MOTIF_BOXES[variant]
    motif = render_motif(event.key, mw, mh, palette)
    c = Canvas(background=WHITE)
    number_box = _LAYOUTS[variant](c, fonts, event, today, motif)
    return c.to_frame(), number_box


# --- the problem card -------------------------------------------------------------------------


def compose_problem(fonts: FontLibrary, problem: Problem, today: date) -> np.ndarray:
    """A tidy one-message card: what is wrong and where to fix it."""
    c = Canvas(background=WHITE)
    f_eye, t_eye, eyebrow = fit_label(fonts, "COUNTDOWN", 300, "sans", "semibold", 16, 4.0)
    draw_tracked(c, M, 56, eyebrow, f_eye, BLACK, tracking=t_eye)
    f_date = fonts.font("mono", 14)
    c.text((W - M, 56), f"{today:%A}, {today:%B} {today.day}", f_date, BLACK, anchor="rs")
    c.rect((M, 70, W - M - 1, 71), fill=BLACK)
    c.rect((M, 150, M + 13, 163), fill=RED)
    span = W - 2 * M - 30
    x = M + 30
    font, lines = fit_block(
        c,
        problem.headline,
        span,
        110,
        lambda s: fonts.font("serif", s, "bold"),
        max_size=44,
        min_size=24,
        max_lines=2,
    )
    y = 140
    lh = c.line_height(font, 1.1)
    for line in lines:
        c.text((x, y), line, font, BLACK, anchor="la")
        y += lh
    f_line = fit_line(lambda s: fonts.font("sans", s, "medium"), problem.line, span, 22, 16)
    c.text((x, y + 28), ellipsize(c, problem.line, f_line, span), f_line, BLACK, anchor="ls")
    if problem.footnote:
        c.rect((M, 326, W - M - 1, 326), fill=BLACK)
        f_foot = fonts.font("mono", 14)
        c.paragraph((M, 340), problem.footnote, f_foot, BLACK, W - 2 * M, max_lines=2)
    return c.to_frame()
