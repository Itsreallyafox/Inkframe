"""Weather glyphs drawn with flat six-colour primitives.

Every glyph is drawn into a square of side ``size`` centred on ``(cx, cy)``
and scales from about 32 px (an hourly strip) to 160 px (a poster).  Line
widths grow with the size so a 48 px icon and a 140 px icon read as the same
drawing.  Nothing is anti-aliased: shapes are Pillow primitives in palette
colours, and composite shapes (clouds, the crescent moon) are built as 1-bit
masks and filled with a solid palette colour through ``Canvas.paste``.

``bg`` is the colour the glyph sits on.  Colours that would vanish against it
are swapped (a white disc on a yellow band, white rain on a blue band, a
yellow moon on black), so the same glyph works on the paper and on the
poster's colour band.

WMO weather codes (Open-Meteo ``weather_code``) map to a glyph kind with
``glyph_for(code, is_day)``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from PIL import Image, ImageDraw

from inkframe.apps.draw import Canvas, rgb
from inkframe.display.palette import BLACK, BLUE, RED, WHITE, YELLOW

#: every glyph kind ``draw_glyph`` understands
KINDS: tuple[str, ...] = (
    "clear",
    "clear-night",
    "partly",
    "partly-night",
    "cloudy",
    "fog",
    "drizzle",
    "rain",
    "sleet",
    "snow",
    "showers",
    "thunder",
)


def glyph_for(code: int | None, is_day: bool = True) -> tuple[str, int]:
    """``(kind, intensity 1..3)`` for a WMO weather code.

    Unknown or missing codes draw as a plain cloud: the honest "something is
    in the sky" fallback.
    """
    if code is None:
        return "cloudy", 1
    c = int(code)
    if c == 0:
        return ("clear" if is_day else "clear-night"), 1
    if c in (1, 2):
        return ("partly" if is_day else "partly-night"), c
    if c == 3:
        return "cloudy", 2
    if c in (45, 48):
        return "fog", 1
    if c in (51, 53, 55):
        return "drizzle", {51: 1, 53: 2, 55: 3}[c]
    if c in (56, 57, 66, 67):
        return "sleet", 2
    if c in (61, 63, 65):
        return "rain", {61: 1, 63: 2, 65: 3}[c]
    if c in (71, 73, 75, 77):
        return "snow", {71: 1, 73: 2, 75: 3, 77: 1}[c]
    if c in (80, 81, 82):
        return ("showers" if is_day else "rain"), {80: 1, 81: 2, 82: 3}[c]
    if c in (85, 86):
        return "snow", 2 if c == 85 else 3
    if c in (95, 96, 99):
        return "thunder", 2
    return "cloudy", 1


# --- colours ------------------------------------------------------------------------


def _inks(bg: int) -> dict[str, int]:
    """Glyph colours that stay visible on background ``bg``."""
    return {
        "line": WHITE if bg in (BLACK, BLUE) else BLACK,
        "outline": BLACK,
        "cloud": WHITE,
        "sun": WHITE if bg == YELLOW else YELLOW,
        "moon": YELLOW if bg in (BLACK, BLUE) else BLACK,
        "rain": WHITE if bg in (BLUE, BLACK) else BLUE,
        "snow": WHITE if bg in (BLUE, BLACK) else BLUE,
        "bolt": YELLOW if bg != YELLOW else WHITE,
    }


def stroke_width(size: int) -> int:
    """Outline width for a glyph of ``size`` px (2 px at 48, 6 px at 140)."""
    return max(2, int(round(size / 24)))


# --- mask helpers ---------------------------------------------------------------------


def _fill_mask(c: Canvas, mask: Image.Image, origin: tuple[int, int], color: int) -> None:
    """Paint ``color`` through a binary ``L`` mask whose top-left is ``origin``."""
    solid = Image.new("RGB", mask.size, rgb(color))
    c.paste(solid, origin, mask)


def _texture_mask(
    c: Canvas, mask: Image.Image, origin: tuple[int, int], a: int, b: int, period: int = 4
) -> None:
    """Paint a 45-degree hatch (one ``a`` pixel line every ``period`` px on ``b``)."""
    w, h = mask.size
    ys, xs = np.mgrid[0:h, 0:w]
    on = ((xs + ys + origin[0] + origin[1]) % period) == 0
    tile = np.where(on[..., None], np.array(rgb(a), np.uint8), np.array(rgb(b), np.uint8))
    c.paste(Image.fromarray(tile.astype(np.uint8), "RGB"), origin, mask)


# --- primitives -------------------------------------------------------------------------


def _disc(c: Canvas, cx: float, cy: float, r: float, fill: int, outline: int | None, lw: int):
    if outline is not None:
        c.ellipse((cx - r - lw, cy - r - lw, cx + r + lw, cy + r + lw), fill=outline)
    c.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)


def _round_line(c: Canvas, p0: tuple[float, float], p1: tuple[float, float], color: int, w: int):
    """A line with round caps (the caps keep short strokes from looking chopped)."""
    c.line([p0, p1], fill=color, width=w)
    r = w / 2 - 0.01
    if r >= 1:
        for x, y in (p0, p1):
            c.ellipse((x - r, y - r, x + r, y + r), fill=color)


def _sun(c: Canvas, cx: float, cy: float, size: float, inks: dict[str, int], rays: bool = True):
    """Disc of radius 0.22*size with eight rays reaching 0.46*size."""
    lw = stroke_width(int(size))
    r = 0.22 * size
    if rays:
        rw = max(2, int(round(size / 17)))
        r0, r1 = 0.33 * size, 0.47 * size
        for k in range(8):
            a = math.pi / 4 * k
            ca, sa = math.cos(a), math.sin(a)
            _round_line(
                c, (cx + r0 * ca, cy + r0 * sa), (cx + r1 * ca, cy + r1 * sa), inks["line"], rw
            )
    _disc(c, cx, cy, r, inks["sun"], inks["line"], lw)


def _moon(c: Canvas, cx: float, cy: float, size: float, color: int) -> None:
    """A crescent opening to the upper right, built as a mask (no background paint)."""
    r = 0.30 * size
    side = int(math.ceil(2 * r)) + 2
    mask = Image.new("L", (side, side), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((1, 1, 1 + 2 * r, 1 + 2 * r), fill=255)
    off_x, off_y, rc = 0.42 * r, -0.30 * r, 0.80 * r
    ccx, ccy = 1 + r + off_x, 1 + r + off_y
    d.ellipse((ccx - rc, ccy - rc, ccx + rc, ccy + rc), fill=0)
    _fill_mask(c, mask, (int(round(cx - r - 1)), int(round(cy - r - 1))), color)


# cloud silhouette in units of its width: (cx, cy, r) puffs and a rounded base
_PUFFS: tuple[tuple[float, float, float], ...] = (
    (0.30, 0.31, 0.17),
    (0.56, 0.23, 0.23),
    (0.79, 0.34, 0.13),
)
_BASE = (0.05, 0.30, 0.95, 0.57)  # x0, y0, x1, y1
CLOUD_ASPECT = 0.57  # height / width


def _cloud_mask(w: float, grow: float, pad: int) -> Image.Image:
    """Binary mask of a cloud ``w`` wide, every shape grown by ``grow`` px."""
    W = int(math.ceil(w)) + 2 * pad
    H = int(math.ceil(w * CLOUD_ASPECT)) + 2 * pad
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    for px, py, pr in _PUFFS:
        x, y, r = pad + px * w, pad + py * w, pr * w + grow
        d.ellipse((x - r, y - r, x + r, y + r), fill=255)
    x0, y0, x1, y1 = (pad + v * w for v in _BASE)
    rad = (y1 - y0) / 2 + grow
    d.rounded_rectangle((x0 - grow, y0 - grow, x1 + grow, y1 + grow), radius=rad, fill=255)
    return mask


def _cloud(
    c: Canvas,
    x0: float,
    y0: float,
    w: float,
    inks: dict[str, int],
    lw: int,
    texture: bool = False,
) -> None:
    """A cloud whose bounding box (without outline) starts at ``(x0, y0)``."""
    pad = lw + 2
    origin = (int(round(x0)) - pad, int(round(y0)) - pad)
    _fill_mask(c, _cloud_mask(w, lw, pad), origin, inks["outline"])
    inner = _cloud_mask(w, 0, pad)
    if texture:
        _texture_mask(c, inner, origin, BLACK, WHITE, period=max(3, int(round(w / 22))))
    else:
        _fill_mask(c, inner, origin, inks["cloud"])


def _outlined_polygon(
    c: Canvas, pts: Sequence[tuple[float, float]], fill: int, outline: int, lw: int
) -> None:
    """A filled polygon with a ``lw`` outline that follows it exactly (the polygon
    grown by a disc: thick edges plus round joints), drawn through masks."""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    pad = lw + 2
    ox, oy = int(math.floor(min(xs))) - pad, int(math.floor(min(ys))) - pad
    size = (int(math.ceil(max(xs))) - ox + pad + 1, int(math.ceil(max(ys))) - oy + pad + 1)
    local = [(x - ox, y - oy) for x, y in pts]
    outer = Image.new("L", size, 0)
    d = ImageDraw.Draw(outer)
    d.polygon(local, fill=255)
    for i, p0 in enumerate(local):
        p1 = local[(i + 1) % len(local)]
        d.line([p0, p1], fill=255, width=2 * lw)
        d.ellipse((p0[0] - lw, p0[1] - lw, p0[0] + lw, p0[1] + lw), fill=255)
    inner = Image.new("L", size, 0)
    ImageDraw.Draw(inner).polygon(local, fill=255)
    _fill_mask(c, outer, (ox, oy), outline)
    _fill_mask(c, inner, (ox, oy), fill)


def _bolt(c: Canvas, x: float, y: float, size: float, inks: dict[str, int], lw: int) -> None:
    """A lightning bolt whose top-centre is ``(x, y)``; height ~0.46*size."""
    s = size
    pts = [
        (0.07, 0.00),
        (-0.09, 0.24),
        (0.01, 0.24),
        (-0.07, 0.46),
        (0.14, 0.16),
        (0.04, 0.16),
        (0.15, 0.00),
    ]
    poly = [(x + px * s - 0.03 * s, y + py * s) for px, py in pts]
    _outlined_polygon(c, poly, inks["bolt"], inks["outline"], max(2, lw - 1))


def _rain(
    c: Canvas,
    xs: Sequence[float],
    y0: float,
    length: float,
    color: int,
    w: int,
    slant: float,
) -> None:
    for i, x in enumerate(xs):
        dy = (i % 2) * length * 0.35
        _round_line(c, (x, y0 + dy), (x - slant, y0 + dy + length), color, w)


def _dots(c: Canvas, pts: Sequence[tuple[float, float]], r: float, inks: dict[str, int], lw: int):
    """Snow: alternating solid dots and outlined white dots."""
    for i, (x, y) in enumerate(pts):
        if i % 2 == 0:
            c.ellipse((x - r, y - r, x + r, y + r), fill=inks["snow"])
        else:
            o = max(1, lw // 2 + 1)
            c.ellipse((x - r, y - r, x + r, y + r), fill=inks["outline"])
            c.ellipse((x - r + o, y - r + o, x + r - o, y + r - o), fill=WHITE)


# --- the glyphs ---------------------------------------------------------------------


def draw_weather(
    c: Canvas,
    code: int | None,
    is_day: bool,
    cx: float,
    cy: float,
    size: int,
    bg: int = WHITE,
) -> str:
    """Draw the glyph for WMO ``code``; returns the kind that was drawn."""
    kind, intensity = glyph_for(code, is_day)
    draw_glyph(c, kind, cx, cy, size, bg=bg, intensity=intensity)
    return kind


def draw_glyph(
    c: Canvas,
    kind: str,
    cx: float,
    cy: float,
    size: int,
    bg: int = WHITE,
    intensity: int = 2,
) -> None:
    """Draw glyph ``kind`` (one of ``KINDS``) in a ``size`` square centred on ``(cx, cy)``."""
    if kind not in KINDS:
        raise ValueError(f"unknown glyph kind {kind!r}")
    s = float(size)
    lw = stroke_width(size)
    inks = _inks(bg)
    small = size < 64

    if kind == "clear":
        _sun(c, cx, cy, s, inks)
        return
    if kind == "clear-night":
        _moon(c, cx + 0.02 * s, cy, s * 1.25, inks["moon"])
        return
    if kind in ("partly", "partly-night"):
        if kind == "partly":
            _sun(c, cx - 0.14 * s, cy - 0.15 * s, 0.70 * s, inks)
        else:
            _moon(c, cx - 0.12 * s, cy - 0.14 * s, 0.82 * s, inks["moon"])
        w = (0.66 if intensity == 1 else 0.78) * s
        _cloud(c, cx + 0.47 * s - w, cy + 0.40 * s - w * CLOUD_ASPECT, w, inks, lw)
        return
    if kind == "cloudy":
        wb = 0.60 * s
        _cloud(c, cx - 0.10 * s, cy - 0.34 * s, wb, inks, lw, texture=True)
        w = 0.84 * s
        _cloud(c, cx - 0.46 * s, cy - 0.12 * s, w, inks, lw)
        return
    if kind == "fog":
        w = 0.80 * s
        _cloud(c, cx - w / 2, cy - 0.40 * s, w, inks, lw)
        bar_h = max(4, int(round(0.09 * s)))
        gap = max(3, int(round(0.06 * s)))
        halo = max(2, gap // 2 + 1)
        y = int(round(cy))
        spans = ((-0.42, 0.34), (-0.32, 0.44), (-0.42, 0.24))
        boxes = []
        for x0f, x1f in spans:
            boxes.append((int(round(cx + x0f * s)), y, int(round(cx + x1f * s)), y + bar_h))
            y += bar_h + gap
        for x0, y0, x1, y1 in boxes:  # a paper halo keeps the bars off the outline/band
            c.rect((x0 - halo, y0 - halo, x1 + halo - 1, y1 + halo - 1), fill=WHITE)
        for box in boxes:  # engraved hairlines: two rows of ink, one of paper
            c.stripes(box, inks["line"], WHITE, period=3, duty=0.67, angle=0)
        return

    # everything else: a cloud with something falling out of it
    if kind == "showers":
        # the sun peeks out above the right shoulder of a smaller cloud
        _sun(c, cx + 0.22 * s, cy - 0.22 * s, 0.56 * s, inks)
        w = 0.76 * s
        top = cy - 0.36 * s
        _cloud(c, cx - 0.48 * s, top, w, inks, lw)
        cx -= 0.08 * s
    else:
        w = 0.86 * s
        top = cy - 0.44 * s
        _cloud(c, cx - w / 2, top, w, inks, lw)
    fall_y = top + w * CLOUD_ASPECT + lw + 0.05 * s
    rw = max(2, int(round(s / 19)))
    if kind in ("rain", "showers"):
        n = (3 if small else 3 + (intensity >= 2)) if intensity < 3 else (4 if small else 5)
        xs = _spread(cx + 0.04 * s, 0.56 * s, n)
        _rain(c, xs, fall_y, 0.20 * s, inks["rain"], rw, 0.07 * s)
    elif kind == "drizzle":
        n = 3 if small else 4
        xs = _spread(cx + 0.02 * s, 0.50 * s, n)
        _rain(c, xs, fall_y, 0.08 * s, inks["rain"], rw, 0.03 * s)
    elif kind == "snow":
        r = max(2.0, 0.045 * s)
        row1 = [(x, fall_y + r + 1) for x in _spread(cx, 0.54 * s, 3 if small else 4)]
        row2 = [(x, fall_y + r + 0.17 * s) for x in _spread(cx, 0.36 * s, 2 if small else 3)]
        _dots(c, row1 + row2, r, inks, lw)
    elif kind == "sleet":
        xs = _spread(cx, 0.54 * s, 4 if small else 5)
        r = max(2.0, 0.045 * s)
        for i, x in enumerate(xs):
            if i % 2 == 0:
                _rain(c, [x + 0.03 * s], fall_y, 0.18 * s, inks["rain"], rw, 0.06 * s)
            else:
                _dots(c, [(x, fall_y + 0.10 * s)], r, inks, lw)
    elif kind == "thunder":
        _bolt(c, cx, fall_y - 0.14 * s, s, inks, lw)


def _spread(center: float, width: float, n: int) -> list[float]:
    if n <= 1:
        return [center]
    step = width / (n - 1)
    return [center - width / 2 + i * step for i in range(n)]


# --- non-weather glyphs for the notice cards ------------------------------------------


def draw_compass(c: Canvas, cx: float, cy: float, size: int, bg: int = WHITE) -> None:
    """A compass rose: ring, cardinal ticks, a red north needle (the 'set a location' card)."""
    s = float(size)
    lw = stroke_width(size)
    ink = _inks(bg)["line"]
    r = 0.44 * s
    c.ellipse((cx - r, cy - r, cx + r, cy + r), outline=ink, width=lw)
    for k in range(4):
        a = math.pi / 2 * k
        ca, sa = math.cos(a), math.sin(a)
        p0 = (cx + (r - 0.12 * s) * ca, cy + (r - 0.12 * s) * sa)
        p1 = (cx + (r - lw) * ca, cy + (r - lw) * sa)
        c.line([p0, p1], fill=ink, width=lw)
    nw, nl = 0.11 * s, 0.30 * s
    c.polygon([(cx, cy - nl), (cx + nw, cy), (cx - nw, cy)], fill=RED)
    c.polygon([(cx, cy + nl), (cx + nw, cy), (cx - nw, cy)], fill=ink)
    hub = max(2.0, 0.035 * s)
    _disc(c, cx, cy, hub, WHITE, ink, max(1, lw // 2))


def draw_offline(c: Canvas, cx: float, cy: float, size: int, bg: int = WHITE) -> None:
    """A cloud struck through in red (the 'no data' card)."""
    s = float(size)
    lw = stroke_width(size)
    inks = _inks(bg)
    w = 0.88 * s
    _cloud(c, cx - w / 2, cy - w * CLOUD_ASPECT / 2, w, inks, lw)
    p0, p1 = (cx - 0.36 * s, cy + 0.36 * s), (cx + 0.36 * s, cy - 0.36 * s)
    _round_line(c, p0, p1, bg, lw * 4)
    _round_line(c, p0, p1, RED, lw * 2)


def draw_pin(c: Canvas, cx: float, cy: float, size: int, bg: int = WHITE) -> None:
    """A map pin with an empty head (the 'place not found' card)."""
    s = float(size)
    lw = stroke_width(size)
    ink = _inks(bg)["line"]
    r = 0.26 * s
    hy = cy - 0.14 * s
    tip = (cx, cy + 0.44 * s)
    # tangent points of the tip on the head circle
    d = tip[1] - hy
    a = math.asin(min(1.0, r / d))
    tx = r * math.cos(a)
    ty = r * math.sin(a)
    left = (cx - tx, hy + ty)
    right = (cx + tx, hy + ty)
    _disc(c, cx, hy, r, RED, ink, lw)
    _outlined_polygon(c, [left, tip, right], RED, ink, lw)
    _disc(c, cx, hy, r, RED, None, lw)
    hole = 0.10 * s
    _disc(c, cx, hy, hole, WHITE, ink, lw)
