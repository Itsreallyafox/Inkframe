"""The weather app's card layouts: three variants plus the notice cards.

All three variants share one grid: a 28 px margin, a mono metadata line
(place, data time) and Inter for every numeral.  Black type on white paper;
each variant has one accent:

* ``now``: a red degree sign on the giant temperature; blue only for rain.
* ``days``: blue precipitation bars (the only colour that is not a glyph).
* ``poster``: a full-bleed band on the left third whose colour *is* the
  weather (yellow sun, blue rain, black clear night, dotted grey otherwise).

Notice cards (no location, place not found, offline without cache) reuse the
header of the ``now`` variant so they read as part of the same family.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from PIL import ImageFont

from inkframe.apps.base import FontLibrary
from inkframe.apps.draw import Canvas
from inkframe.apps.weather_glyphs import (
    draw_compass,
    draw_offline,
    draw_pin,
    draw_weather,
)
from inkframe.apps.weather_model import (
    DASH,
    DEGREE,
    Weather,
    band_for,
    condition_text,
    fmt_clock,
    fmt_date,
    fmt_day,
    fmt_hour,
    fmt_temp,
    round_half_up,
)
from inkframe.display.palette import BLACK, BLUE, RED, WHITE, YELLOW

W, H = 600, 400
M = 28  # outer margin
Font = ImageFont.FreeTypeFont


@dataclass
class Style:
    """Font access plus the unit conventions of one render."""

    fonts: FontLibrary
    imperial: bool = False

    def sans(self, size: int, weight: str = "regular") -> Font:
        return self.fonts.font("sans", size, weight)

    def mono(self, size: int, weight: str = "regular") -> Font:
        return self.fonts.font("mono", size, weight)

    def serif(self, size: int, weight: str = "regular") -> Font:
        return self.fonts.font("serif", size, weight)

    @property
    def wind_unit(self) -> str:
        return "MPH" if self.imperial else "KM/H"

    def clock(self, t: datetime | None) -> str:
        return fmt_clock(t, self.imperial)


# --- text helpers ------------------------------------------------------------------------


def ink_left(font: Font, text: str, anchor: str = "ls") -> int:
    """Offset from the anchor to the left edge of the ink (side bearing)."""
    return int(font.getbbox(text, anchor=anchor)[0])


def text_at(
    c: Canvas,
    x: float,
    y: float,
    text: str,
    font: Font,
    fill: int,
    anchor: str = "ls",
    optical: bool = False,
) -> tuple[int, int, int, int]:
    """``Canvas.text``; ``optical=True`` puts the ink (not the advance box) at ``x``,
    which matters for big numerals whose side bearing is several pixels."""
    if optical:
        x -= ink_left(font, text, anchor)
    return c.text((x, y), text, font, fill, anchor=anchor)


def fit_font(make, text: str, max_width: float, max_size: int, min_size: int) -> Font:
    """Largest ``make(size)`` font at which ``text`` fits ``max_width``."""
    size = max_size
    while size > min_size:
        f = make(size)
        if f.getlength(text) <= max_width:
            return f
        size -= 2 if size > 40 else 1
    return make(min_size)


def ellipsize(text: str, font: Font, max_width: float) -> str:
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "\u2026") > max_width:
        text = text[:-1].rstrip(" ,")
    return text + "\u2026"


def meta_line(w: Weather, now: datetime, style: Style) -> tuple[str, int]:
    """``("UPDATED 21:00", BLACK)`` or ``("OFFLINE, DATA FROM 13:50", RED)``.

    The stamp is in the forecast location's local time, like every other time
    on the card; a day name is added when the data is not from today."""
    t = w.local_time(w.fetched_at)
    stamp = style.clock(t)
    if t.date() != w.local_now(now).date():
        stamp = f"{fmt_day(t.date())} {stamp}"
    if w.offline:
        return f"OFFLINE, DATA FROM {stamp}", RED
    return f"UPDATED {stamp}", BLACK


def _big_temperature(
    c: Canvas,
    x: float,
    baseline: float,
    value: float | None,
    style: Style,
    font: Font,
    degree_fill: int,
) -> int:
    """Giant numeral with a smaller degree sign hung from the cap line (a full-size
    degree at 170 px is a fat ring that outweighs the figure).  Returns the right edge."""
    num = fmt_temp(value, degree=False)
    text_at(c, x, baseline, num, font, BLACK, optical=True)
    right = x - ink_left(font, num) + font.getlength(num)
    if value is None:
        return int(right)
    cap_top = baseline + font.getbbox("8", anchor="ls")[1]
    f_deg = style.sans(max(14, int(font.size * 0.62)), "bold")
    deg_top = f_deg.getbbox(DEGREE, anchor="ls")[1]
    gap = font.size * 0.03
    box = text_at(c, right + gap, cap_top - deg_top, DEGREE, f_deg, degree_fill, optical=True)
    return box[2]


def _temp_font(style: Style, text: str, max_width: float, max_size: int, min_size: int) -> Font:
    return fit_font(lambda s: style.sans(s, "bold"), text, max_width, max_size, min_size)


def display_face(style: Style, text: str):
    """Playfair Bold for words; Inter Bold as soon as figures appear (Playfair's
    old-style figures dip below the baseline and look broken at display size)."""
    if any(ch.isdigit() for ch in text):
        return lambda s: style.sans(s, "bold")
    return lambda s: style.serif(s, "bold")


SEP = "  \u00b7  "


def _header(c: Canvas, style: Style, left: str, right: list[tuple[str, int]]) -> None:
    """The family header: place on the left (ellipsized to fit), right-aligned
    segments (date, data time) on the right, a 2 px rule under both."""
    f_left = style.mono(15, "bold")
    f_right = style.mono(15)
    x = W - M
    for text, fill in reversed(right):
        c.text((x, 40), text, f_right, fill, anchor="rs")
        x -= f_right.getlength(text)
    c.text((M, 40), ellipsize(left.upper(), f_left, x - M - 24), f_left, BLACK, anchor="ls")
    c.rect((M, 52, W - M - 1, 53), fill=BLACK)


# --- variant 0: now --------------------------------------------------------------------------


def render_now(c: Canvas, w: Weather, now: datetime, style: Style) -> None:
    cur = w.conditions(now)
    today = w.today(now)
    local = w.local_now(now)
    meta, meta_fill = meta_line(w, now, style)

    # header: place left; date + data time right.  Offline, the red stamp is the
    # news and the date steps aside so the place keeps its room.
    right = (
        [(meta, meta_fill)] if w.offline else [(fmt_date(local.date()) + SEP, BLACK), (meta, BLACK)]
    )
    _header(c, style, w.place.display, right)

    # the giant temperature (shrinks for "−12°" or "104°") and the glyph beside it
    sample = fmt_temp(cur.temperature)
    f_big = _temp_font(style, sample, 340, 172, 112)
    _big_temperature(c, M, 192, cur.temperature, style, f_big, RED)
    gx = W - M - 74
    draw_weather(c, cur.code, cur.is_day, gx, 130, 148)

    # condition (left) and hi/lo (right, under the glyph) share a baseline
    f_cond = style.sans(30, "semibold")
    cond = ellipsize(condition_text(cur.code, cur.is_day), f_cond, 330)
    c.text((M, 236), cond, f_cond, BLACK, anchor="ls")
    if today is not None:
        f_hl = style.sans(24, "medium")
        hl = f"H {fmt_temp(today.high)}   L {fmt_temp(today.low)}"
        c.text((W - M, 236), hl, f_hl, BLACK, anchor="rs")

    # the spec row
    f_lbl = style.mono(15)
    f_val = style.mono(15, "bold")
    x = M
    parts = [
        ("FEELS ", fmt_temp(cur.apparent)),
        ("HUMIDITY ", DASH if cur.humidity is None else f"{round_half_up(cur.humidity)}%"),
        ("WIND ", DASH if cur.wind is None else f"{round_half_up(cur.wind)} {style.wind_unit}"),
    ]
    for label, value in parts:
        c.text((x, 264), label, f_lbl, BLACK, anchor="ls")
        x += f_lbl.getlength(label)
        c.text((x, 264), value, f_val, BLACK, anchor="ls")
        x += f_val.getlength(value) + 30

    # six-hour strip: hour, glyph, temperature (+ rain chance in blue when it matters)
    c.rect((M, 284, W - M - 1, 284), fill=BLACK)
    hours = w.next_hours(now, 6)
    if not hours:
        c.text((M, 340), "NO HOURLY FORECAST IN THE CACHED DATA", f_lbl, BLACK, anchor="ls")
        return
    cw = (W - 2 * M) / 6
    f_hour = style.mono(14)
    f_t = style.sans(24, "semibold")
    f_p = style.mono(14, "bold")
    for i, h in enumerate(hours):
        cx = M + cw * i + cw / 2
        c.text((cx, 304), fmt_hour(h.time, style.imperial), f_hour, BLACK, anchor="ms")
        draw_weather(c, h.code, h.is_day, cx, 331, 44)
        t_txt = fmt_temp(h.temperature)
        rain = h.precip is not None and h.precip >= 30
        p_txt = f"{round_half_up(h.precip)}%" if rain else ""
        tw = f_t.getlength(t_txt)
        pw = f_p.getlength(p_txt) + 5 if rain else 0
        x0 = cx - (tw + pw) / 2
        c.text((x0, 378), t_txt, f_t, BLACK, anchor="ls")
        if rain:
            c.text((x0 + tw + 5, 378), p_txt, f_p, BLUE, anchor="ls")


# --- variant 1: five days ----------------------------------------------------------------------


def render_days(c: Canvas, w: Weather, now: datetime, style: Style) -> None:
    cur = w.conditions(now)
    local = w.local_now(now)
    meta, meta_fill = meta_line(w, now, style)

    # slim header: place + data time on the left, current temp + glyph on the right
    f_now = style.sans(50, "bold")
    t_now = fmt_temp(cur.temperature)
    now_w = f_now.getlength(t_now)
    c.text((W - M, 76), t_now, f_now, BLACK, anchor="rs")
    draw_weather(c, cur.code, cur.is_day, W - M - now_w - 42, 56, 52)
    avail = W - 2 * M - now_w - 100
    f_place = fit_font(lambda s: style.sans(s, "bold"), w.place.display, avail, 30, 20)
    c.text((M, 52), ellipsize(w.place.display, f_place, avail), f_place, BLACK, anchor="ls")
    c.text((M, 76), meta, style.mono(14), meta_fill, anchor="ls")
    c.rect((M, 90, W - M - 1, 92), fill=BLACK)

    days = w.next_days(now, 5)
    if not days:
        c.text((M, 200), "NO DAILY FORECAST IN THE CACHED DATA", style.mono(15), BLACK)
        return
    cw = (W - 2 * M) / len(days)  # an old cache may have fewer days left: spread them
    f_day = style.mono(16, "bold")
    f_hi = style.sans(40, "bold")
    f_lo = style.sans(26)
    f_p = style.mono(14)
    for i, d in enumerate(days):
        x0 = M + cw * i
        cx = x0 + cw / 2
        if i:
            c.rect((int(x0), 108, int(x0), 374), fill=BLACK)
        label = "TODAY" if d.date == local.date() else fmt_day(d.date)
        if label == "TODAY":
            lw = f_day.getlength(label)
            c.rect((int(cx - lw / 2 - 8), 108, int(cx + lw / 2 + 8), 132), fill=BLACK)
            c.text((cx, 126), label, f_day, WHITE, anchor="ms")
        else:
            c.text((cx, 126), label, f_day, BLACK, anchor="ms")
        draw_weather(c, d.code, True, cx, 186, 76)
        c.text((cx, 276), fmt_temp(d.high), f_hi, BLACK, anchor="ms")
        c.text((cx, 310), fmt_temp(d.low), f_lo, BLACK, anchor="ms")
        # precipitation probability: a blue meter in a hairline box
        bw, bh, by = 66, 12, 330
        bx0 = int(cx - bw / 2)
        c.rect((bx0, by, bx0 + bw - 1, by + bh - 1), outline=BLACK, width=1)
        if d.precip is not None and d.precip >= 5:
            fill_w = max(4, int(round((bw - 4) * min(100.0, d.precip) / 100)))
            c.rect((bx0 + 2, by + 2, bx0 + 1 + fill_w, by + bh - 3), fill=BLUE)
        p_txt = DASH if d.precip is None else f"{round_half_up(d.precip)}%"
        c.text((cx, 366), p_txt, f_p, BLUE if (d.precip or 0) >= 30 else BLACK, anchor="ms")


# --- variant 2: poster ---------------------------------------------------------------------------


_BAND_COLOURS = {"sun": YELLOW, "rain": BLUE, "night": BLACK}


def render_poster(c: Canvas, w: Weather, now: datetime, style: Style) -> None:
    cur = w.conditions(now)
    today = w.today(now)
    meta, meta_fill = meta_line(w, now, style)

    band = band_for(cur.code, cur.is_day)
    bx = 204
    if band in _BAND_COLOURS:
        bg = _BAND_COLOURS[band]
        c.rect((0, 0, bx - 1, H - 1), fill=bg)
    else:
        bg = WHITE
        c.dots((0, 0, bx, H), BLACK, background=WHITE, spacing=6, radius=1)
        c.rect((bx - 3, 0, bx - 1, H - 1), fill=BLACK)
    if band == "night":
        for sx, sy, r in ((44, 70, 3), (150, 104, 2), (60, 300, 2), (162, 316, 3), (120, 54, 2)):
            c.ellipse((sx - r, sy - r, sx + r, sy + r), fill=WHITE)
    draw_weather(c, cur.code, cur.is_day, bx // 2, H // 2, 156, bg=bg)

    x = bx + 32
    right = W - M
    span = right - x
    # kicker (region) and the place name, set large in the display serif
    f_kick = style.mono(14, "bold")
    region = w.place.region.upper() if w.place.region else "WEATHER"
    c.text((x, 44), ellipsize(region, f_kick, span), f_kick, BLACK, anchor="ls")
    name = w.place.name
    f_name = fit_font(display_face(style, name), name, span, 58, 30)
    text_at(c, x, 104, ellipsize(name, f_name, span), f_name, BLACK, optical=True)

    f_big = _temp_font(style, fmt_temp(cur.temperature), span, 150, 100)
    _big_temperature(c, x, 250, cur.temperature, style, f_big, BLACK)

    f_cond = style.serif(34, "italic")
    cond = ellipsize(condition_text(cur.code, cur.is_day), f_cond, span)
    c.text((x, 296), cond, f_cond, BLACK, anchor="ls")

    c.rect((x, 316, right - 1, 316), fill=BLACK)
    f_lbl = style.mono(14)
    f_val = style.mono(15, "bold")
    if today is not None:
        xx = x
        for label, t in (("SUNRISE ", today.sunrise), ("SUNSET ", today.sunset)):
            c.text((xx, 344), label, f_lbl, BLACK, anchor="ls")
            xx += f_lbl.getlength(label)
            txt = style.clock(t)
            c.text((xx, 344), txt, f_val, BLACK, anchor="ls")
            xx += f_val.getlength(txt) + 26
    c.text((x, 372), meta, f_lbl, meta_fill, anchor="ls")
    if today is not None:
        hl = f"H {fmt_temp(today.high)}  L {fmt_temp(today.low)}"
        if f_lbl.getlength(meta) + f_val.getlength(hl) + 20 <= span:
            c.text((right, 372), hl, f_val, BLACK, anchor="rs")


# --- notice cards -----------------------------------------------------------------------------


def render_notice(
    c: Canvas,
    style: Style,
    now: datetime,
    *,
    glyph: str,
    headline: str,
    line: str,
    place: str = "",
    footnote: str = "",
    line_fill: int = BLACK,
) -> None:
    """A tidy one-message card: header, a glyph, a headline and one line."""
    _header(c, style, place or "WEATHER", [(fmt_date(now.date()) + SEP + style.clock(now), BLACK)])
    draw = {"compass": draw_compass, "offline": draw_offline, "pin": draw_pin}[glyph]
    draw(c, 116, 200, 132)
    x = 220
    span = W - M - x
    f_head = fit_font(display_face(style, headline), headline, span, 46, 26)
    c.text((x, 194), ellipsize(headline, f_head, span), f_head, BLACK, anchor="ls")
    f_line = fit_font(lambda s: style.sans(s, "medium"), line, span, 22, 16)
    c.text((x, 234), ellipsize(line, f_line, span), f_line, line_fill, anchor="ls")
    if footnote:
        c.rect((M, 334, W - M - 1, 334), fill=BLACK)
        f_foot = style.mono(14)
        c.text((M, 362), ellipsize(footnote.upper(), f_foot, W - 2 * M), f_foot, BLACK, anchor="ls")
