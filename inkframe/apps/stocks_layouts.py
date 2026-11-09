"""The Stocks cards: three layouts and the notice cards, palette-pure.

A quiet white card: black type, the day change in GREEN (up), RED (down) or
BLACK (flat), each sparkline a 2 px black line with its last point marked in
the change colour and a 1 px BLUE baseline at the previous close.  All three
share the family header ("MARKETS" and the status line over a rule) and the
footer (the data stamp on the left, the sparkline span on the right):

0. **board**: two columns of up to six rows, each a label, the price, the
   change and a 90 x 24 sparkline (the change shrinks a size, then the price
   is ellipsized, when the two would meet: ``fit_row``).
1. **hero**: the first three indices (``^`` symbols, else the first three
   entries) large across the top with wide sparklines, the rest in a strip
   of small tiles below.
2. **tiles**: a 4 x 3 grid of tiles; empty cells keep a faint dotted rule.

Every layout fits twelve symbols with nothing under 14 px.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from PIL import ImageFont

from inkframe.apps.base import FontLibrary
from inkframe.apps.draw import Canvas
from inkframe.apps.stocks_data import (
    DASH,
    DOT,
    RANGE_LABELS,
    WEEKDAYS,
    Board,
    Item,
    Quote,
    fmt_change,
    fmt_clock,
    fmt_price,
    local,
    stamp_line,
    status_line,
)
from inkframe.display.palette import BLACK, BLUE, GREEN, RED, WHITE

W, H = 600, 400
M = 28  # outer margin
Font = ImageFont.FreeTypeFont
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
#: layout ids in variant order
LAYOUTS: tuple[str, ...] = ("board", "hero", "tiles")
#: "no data" for a symbol that failed while the others came through
NO_DATA = "no data"


@dataclass
class Style:
    fonts: FontLibrary

    def sans(self, size: int, weight: str = "regular") -> Font:
        return self.fonts.font("sans", size, weight)

    def mono(self, size: int, weight: str = "regular") -> Font:
        return self.fonts.font("mono", size, weight)

    def serif(self, size: int, weight: str = "regular") -> Font:
        return self.fonts.font("serif", size, weight)


# --- shared pieces ----------------------------------------------------------------------


def ellipsize(text: str, font: Font, max_width: float) -> str:
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "…") > max_width:
        text = text[:-1].rstrip(" ,")
    return text + "…"


def fit_font(make: Callable[[int], Font], text: str, max_width: float, hi: int, lo: int) -> Font:
    """Largest ``make(size)`` font at which ``text`` fits ``max_width``."""
    size = hi
    while size > lo:
        f = make(size)
        if f.getlength(text) <= max_width:
            return f
        size -= 2 if size > 30 else 1
    return make(lo)


def change_colour(q: Quote | None) -> int:
    if q is None:
        return BLACK
    return {1: GREEN, -1: RED}.get(q.direction, BLACK)


def header(c: Canvas, style: Style, right: str, left: str = "MARKETS") -> None:
    """The family header: the title on the left, the status line on the right, a
    2 px rule under both."""
    f_left = style.mono(15, "bold")
    f_right = style.mono(15)
    rw = f_right.getlength(right) if right else 0
    if right:
        c.text((W - M, 40), right, f_right, BLACK, anchor="rs")
    c.text((M, 40), ellipsize(left, f_left, W - 2 * M - rw - 24), f_left, BLACK, anchor="ls")
    c.rect((M, 52, W - M - 1, 53), fill=BLACK)


def footer(c: Canvas, style: Style, board: Board, now: datetime, yrange: str) -> None:
    """The data stamp bottom left (red when stale) and the sparkline span bottom right."""
    f = style.mono(14)
    stamp, fill = stamp_line(board, now)
    span = RANGE_LABELS.get(yrange, "")
    if span:
        c.text((W - M, 384), span, f, BLACK, anchor="rs")
    if stamp:
        room = W - 2 * M - (f.getlength(span) + 24 if span else 0)
        c.text((M, 384), ellipsize(stamp, f, room), f, fill, anchor="ls")


def sparkline(
    c: Canvas,
    box: tuple[int, int, int, int],
    q: Quote,
    *,
    width: int = 2,
    baseline: bool = True,
) -> None:
    """The closes scaled to ``box`` (min/max with 10 % padding), a 1 px BLUE
    baseline at the previous close where it falls inside the box, the line in
    BLACK and the last point marked in the change colour.  One close is a flat
    line."""
    x0, y0, x1, y1 = box
    closes = list(q.closes) or [q.price]
    r = 3  # the marker's radius: keep the line inside the box
    x1 -= r
    y0 += r
    y1 -= r
    lo, hi = min(closes), max(closes)
    span = hi - lo
    if span <= 0:
        span = abs(hi) * 0.02 or 1.0
    lo -= span * 0.1
    hi += span * 0.1

    def ypos(v: float) -> float:
        return y1 - (v - lo) / (hi - lo) * (y1 - y0)

    if len(closes) == 1:
        pts = [(float(x0), ypos(closes[0])), (float(x1), ypos(closes[0]))]
    else:
        step = (x1 - x0) / (len(closes) - 1)
        pts = [(x0 + i * step, ypos(v)) for i, v in enumerate(closes)]
    if baseline and q.prev_close is not None and lo <= q.prev_close <= hi:
        yb = round(ypos(q.prev_close))
        c.line([(x0, yb), (x1 + r, yb)], BLUE, 1)
    c.line(pts, BLACK, width)
    lx, ly = pts[-1]
    c.ellipse((lx - r, ly - r, lx + r, ly + r), fill=change_colour(q))


def fit_row(
    style: Style, price: str, change: str, room: float, *, gap: int = 6
) -> tuple[str, Font, Font]:
    """The price and the change of a board row on one baseline: mono 16 and 15
    when both fit ``room`` with ``gap`` between them, else the change drops to
    mono 14 and, if that is still not enough, the price is ellipsized.  Returns
    the price text to draw and the two fonts.  (``9,999.99`` beside
    ``−1,234.56`` is 161 px at 16/15 in a 158 px two-column slot.)"""
    f_price = style.mono(16)
    for size in (15, 14):
        f_change = style.mono(size)
        if f_price.getlength(price) + f_change.getlength(change) + gap <= room:
            return price, f_price, f_change
    return ellipsize(price, f_price, room - f_change.getlength(change) - gap), f_price, f_change


def _heroes(items: list[Item]) -> tuple[list[Item], list[Item]]:
    """The indices (``^`` symbols) first, filled up to three with the first other
    entries; the rest in their order."""
    heroes = [i for i in items if i.symbol.startswith("^")][:3]
    for item in items:
        if len(heroes) >= 3:
            break
        if item not in heroes:
            heroes.append(item)
    rest = [i for i in items if i not in heroes]
    return heroes, rest


# --- variant 0: board ---------------------------------------------------------------------


def render_board(
    c: Canvas, board: Board, now: datetime, style: Style, *, percent: bool, yrange: str
) -> None:
    header(c, style, status_line(board, now))
    footer(c, style, board, now, yrange)
    items = board.items[:12]
    n = len(items)
    # two columns of six; up to six symbols get one wide column of taller rows
    cols = 2 if n > 6 else 1
    rows = 6 if cols == 2 else max(1, n)
    gap = 20
    col_w = (W - 2 * M - gap * (cols - 1)) // cols
    top, area_h = 66, 288
    row_h = min(144 if n == 1 else 96, area_h // rows)
    f_label = style.sans(16, "semibold")
    f_price = style.mono(16)
    f_none = style.mono(14)
    spark_w = 90 if cols == 2 else 240
    spark_h = min(100, max(24, row_h - 20))
    dy = (row_h - 48) // 2  # the two text lines sit mid-row when rows are tall
    for k, item in enumerate(items):
        col, row = divmod(k, rows)
        x0 = M + col * (col_w + gap)
        x1 = x0 + col_w
        y = top + row * row_h
        text_right = x1 - spark_w - 14
        q = item.quote
        c.text(
            (x0, y + dy + 18), ellipsize(item.label, f_label, text_right - x0), f_label, BLACK, "ls"
        )
        if q is None:
            c.text((x0, y + dy + 40), DASH, f_price, BLACK, anchor="ls")
            c.text((text_right, y + dy + 40), NO_DATA, f_none, BLACK, anchor="rs")
        else:
            change = fmt_change(q, percent)
            price, f_p, f_change = fit_row(style, fmt_price(q.price), change, text_right - x0)
            c.text((x0, y + dy + 40), price, f_p, BLACK, anchor="ls")
            c.text((text_right, y + dy + 40), change, f_change, change_colour(q), "rs")
            sy = y + (row_h - spark_h) // 2
            sparkline(c, (x1 - spark_w, sy, x1, sy + spark_h), q)
        if row < rows - 1 and k + 1 < n:
            c.stripes(
                (x0, y + row_h - 1, x1, y + row_h), BLACK, WHITE, period=4, duty=0.25, angle=90
            )


# --- variant 1: hero ------------------------------------------------------------------------


def render_hero(
    c: Canvas, board: Board, now: datetime, style: Style, *, percent: bool, yrange: str
) -> None:
    header(c, style, status_line(board, now))
    footer(c, style, board, now, yrange)
    heroes, rest = _heroes(board.items)
    n = len(heroes)
    gap = 16
    tile_w = (W - 2 * M - gap * (n - 1)) / n
    top = 66
    hero_bottom = 226 if rest else 350
    f_label = style.sans(16, "semibold")
    f_change = style.mono(16)
    f_none = style.mono(14)
    for i, item in enumerate(heroes):
        x0 = int(round(M + i * (tile_w + gap)))
        x1 = int(round(M + i * (tile_w + gap) + tile_w))
        q = item.quote
        c.text((x0, top + 18), ellipsize(item.label, f_label, x1 - x0), f_label, BLACK, "ls")
        if q is None:
            c.text((x0, top + 62), DASH, style.sans(40, "bold"), BLACK, anchor="ls")
            c.text((x0, top + 86), NO_DATA, f_none, BLACK, anchor="ls")
            continue
        price = fmt_price(q.price)
        f_price = fit_font(lambda s: style.sans(s, "bold"), price, x1 - x0, 40, 24)
        c.text((x0, top + 62), price, f_price, BLACK, anchor="ls")
        c.text((x0, top + 86), fmt_change(q, percent), f_change, change_colour(q), "ls")
        sparkline(c, (x0, top + 98, x1, hero_bottom), q)
    if not rest:
        return
    # the strip: up to five small tiles per row, two rows at most
    per_row = 5
    strip_top = 236
    c.rect((M, strip_top, W - M - 1, strip_top), fill=BLACK)
    sgap = 12
    sw = (W - 2 * M - sgap * (per_row - 1)) / per_row
    row_h = 58
    f_slabel = style.sans(14, "semibold")
    f_sprice = style.mono(15)
    f_schange = style.mono(14)
    for i, item in enumerate(rest[: per_row * 2]):
        row, col = divmod(i, per_row)
        x0 = int(round(M + col * (sw + sgap)))
        x1 = int(round(M + col * (sw + sgap) + sw))
        y = strip_top + 6 + row * (row_h + 6)
        q = item.quote
        c.text((x0, y + 16), ellipsize(item.label, f_slabel, x1 - x0), f_slabel, BLACK, "ls")
        if q is None:
            c.text((x0, y + 36), DASH, f_sprice, BLACK, anchor="ls")
            c.text((x0, y + 54), NO_DATA, f_none, BLACK, anchor="ls")
            continue
        c.text(
            (x0, y + 36), ellipsize(fmt_price(q.price), f_sprice, x1 - x0), f_sprice, BLACK, "ls"
        )
        c.text((x0, y + 54), fmt_change(q, percent), f_schange, change_colour(q), "ls")


# --- variant 2: tiles -----------------------------------------------------------------------


def render_tiles(
    c: Canvas, board: Board, now: datetime, style: Style, *, percent: bool, yrange: str
) -> None:
    header(c, style, status_line(board, now))
    footer(c, style, board, now, yrange)
    cols, rows, gap = 4, 3, 12
    tile_w = (W - 2 * M - gap * (cols - 1)) // cols
    top, tile_h = 66, 90
    f_label = style.sans(15, "semibold")
    f_change = style.mono(14)
    f_price = style.mono(18)
    for n in range(cols * rows):
        row, col = divmod(n, cols)
        x0 = M + col * (tile_w + gap)
        x1 = x0 + tile_w
        y = top + row * (tile_h + gap)
        if n >= len(board.items):
            # an empty cell keeps a faint dotted rule where its price would sit
            c.stripes((x0, y + 41, x1, y + 42), BLACK, WHITE, period=4, duty=0.25, angle=90)
            continue
        item = board.items[n]
        q = item.quote
        change = fmt_change(q, percent) if q is not None else NO_DATA
        cw = f_change.getlength(change)
        c.text((x1, y + 16), change, f_change, change_colour(q), anchor="rs")
        c.text((x0, y + 16), ellipsize(item.label, f_label, tile_w - cw - 8), f_label, BLACK, "ls")
        if q is None:
            c.text((x0, y + 44), DASH, f_price, BLACK, anchor="ls")
            continue
        c.text((x0, y + 44), ellipsize(fmt_price(q.price), f_price, tile_w), f_price, BLACK, "ls")
        sparkline(c, (x0, y + 54, x1, y + tile_h), q)


RENDERERS = (render_board, render_hero, render_tiles)


# --- notice cards -------------------------------------------------------------------------


def _date_line(now: datetime) -> str:
    t = local(now, now)
    return f"{WEEKDAYS[t.weekday()]} {t.day} {MONTHS[t.month - 1]}  {DOT}  {fmt_clock(t)}"


def render_notice(
    c: Canvas,
    style: Style,
    now: datetime,
    *,
    headline: str,
    line: str,
    footnote: str = "",
    line_fill: int = BLACK,
) -> None:
    """A tidy one-message card in the family header: a flat-line glyph, a
    headline, one line and a footnote."""
    header(c, style, _date_line(now))
    # the glyph: an empty chart frame with a flat dashed line
    gx, gy, gw, gh = M + 8, 150, 150, 100
    c.rect((gx, gy, gx + gw, gy + gh), outline=BLACK, width=2)
    for x in range(gx + 10, gx + gw - 10, 16):
        c.line([(x, gy + gh // 2), (x + 8, gy + gh // 2)], BLACK, 2)
    c.ellipse((gx + gw - 16, gy + gh // 2 - 4, gx + gw - 8, gy + gh // 2 + 4), fill=RED)
    x = 220
    span = W - M - x
    f_head = fit_font(lambda s: style.serif(s, "bold"), headline, span, 46, 26)
    c.text((x, 194), ellipsize(headline, f_head, span), f_head, BLACK, anchor="ls")
    f_line = fit_font(lambda s: style.sans(s, "medium"), line, span, 22, 16)
    c.text((x, 234), ellipsize(line, f_line, span), f_line, line_fill, anchor="ls")
    if footnote:
        c.rect((M, 334, W - M - 1, 334), fill=BLACK)
        f_foot = style.mono(14)
        c.text((M, 362), ellipsize(footnote.upper(), f_foot, W - 2 * M), f_foot, BLACK, anchor="ls")
