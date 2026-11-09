"""The twelve Countdown motifs: full-colour drawings dithered for the panel.

Each motif is painted into a ``Scene`` (an RGB float array plus a coverage
mask) with colours *derived from the palette it will be dithered against*:
flat parts use an exact palette colour (``Ink.red``), which the ordered
blue-noise dither leaves perfectly flat, while tones the panel does not have
(orange, pink, lavender, brown) are mixes of two palette colours and come
out as even blue-noise grain.  Round things get a soft radial highlight so
the dithering has tone to show.  Shapes are drawn through Pillow masks at
the final size, without anti-aliasing: a crisp aliased edge on e-ink beats
a speckled soft one.

``render_motif(key, width, height, palette)`` returns the dithered palette
indices and the coverage mask; the layouts paste the indices through the
mask so the card's white stays untouched around the drawing.  The motifs are
deterministic: the scatter uses ``random.Random`` seeded from the key and
the box size, so the same event always gets the same picture.

The New Year motif leaves the middle clear: the layout sets the coming
year's number there in crisp type (``countdown_layouts``).
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw

from inkframe.display.palette import (
    BLACK,
    BLUE,
    GREEN,
    MEASURED_DEFAULT,
    RED,
    WHITE,
    YELLOW,
    Palette,
)

#: the engine method the motifs use.  Atkinson error diffusion reproduces the
#: mixed tones faithfully (ordered dithering turns a pastel into grey grain
#: against this palette); the pixels painted in an exact palette colour are
#: re-imposed afterwards so flat parts stay flat (``render_motif``).
DITHER_METHOD = "atkinson"
DITHER_STRENGTH = 1.0

Point = tuple[float, float]
Box = tuple[int, int, int, int]
Colour = np.ndarray


@dataclass(frozen=True)
class Motif:
    """A dithered motif: ``indices`` (h, w) uint8 and where to paste it.

    ``caption_box`` is where the layout may letter a crisp caption in
    ``caption_fill`` (New Year's year, the Labor Day ribbon's words); ``None``
    for motifs without one.
    """

    indices: np.ndarray
    mask: np.ndarray
    caption_box: Box | None = None
    caption_fill: int = BLACK

    @property
    def size(self) -> tuple[int, int]:
        return self.indices.shape[1], self.indices.shape[0]


# --- colours --------------------------------------------------------------------------------


def mix(a: Colour, b: Colour, t: float) -> Colour:
    return a * (1.0 - t) + b * t


class Ink:
    """Drawing colours derived from the target palette."""

    def __init__(self, palette: Palette) -> None:
        rgb = [np.array(c, dtype=np.float32) for c in palette.rgb]
        self.black, self.white = rgb[BLACK], rgb[WHITE]
        self.yellow, self.red, self.blue, self.green = rgb[YELLOW], rgb[RED], rgb[BLUE], rgb[GREEN]
        self.orange = mix(self.red, self.yellow, 0.5)
        self.light_orange = mix(self.orange, self.white, 0.3)
        self.dark_orange = mix(self.orange, self.black, 0.25)
        self.pink = mix(self.red, self.white, 0.5)
        self.light_pink = mix(self.red, self.white, 0.72)
        self.dark_red = mix(self.red, self.black, 0.3)
        self.light_green = mix(self.green, self.white, 0.45)
        self.dark_green = mix(self.green, self.black, 0.35)
        self.lavender = mix(self.blue, self.white, 0.62)
        self.sky = mix(self.blue, self.white, 0.8)
        self.navy = mix(self.blue, self.black, 0.35)
        self.brown = mix(self.orange, self.black, 0.5)
        self.tan = mix(self.orange, self.white, 0.55)
        self.cream = mix(self.yellow, self.white, 0.72)
        self.gold = mix(self.yellow, self.red, 0.22)
        self.grey = mix(self.black, self.white, 0.55)
        self.light_grey = mix(self.black, self.white, 0.8)


# --- the scene ----------------------------------------------------------------------------


class Scene:
    """An RGB float canvas with a coverage mask; shapes are painted through
    Pillow masks so every pixel is either a flat colour or a computed gradient."""

    def __init__(self, width: int, height: int, ink: Ink) -> None:
        self.w, self.h = int(width), int(height)
        self.ink = ink
        self.rgb = np.empty((self.h, self.w, 3), dtype=np.float32)
        self.rgb[:] = ink.white
        self.alpha = np.zeros((self.h, self.w), dtype=bool)
        self._yy, self._xx = np.mgrid[0 : self.h, 0 : self.w]
        #: ``(box, palette index)`` where a layout may letter a caption
        self.caption: tuple[Box, int] | None = None

    # --- masks ---------------------------------------------------------------------
    def mask(self, draw: Callable[[ImageDraw.ImageDraw], None]) -> np.ndarray:
        m = Image.new("L", (self.w, self.h), 0)
        draw(ImageDraw.Draw(m))
        return np.asarray(m, dtype=np.uint8) > 0

    def paint(
        self,
        mask: np.ndarray,
        colour: Colour,
        *,
        highlight: tuple[float, float, float] | None = None,
        light: float = 0.45,
        shadow: float = 0.22,
    ) -> None:
        """Fill ``mask`` with ``colour``; with ``highlight = (hx, hy, reach)`` the
        colour lightens toward the highlight point and darkens away from it."""
        if not mask.any():
            return
        if highlight is None:
            self.rgb[mask] = colour
        else:
            hx, hy, reach = highlight
            d = np.hypot(self._xx[mask] - hx, self._yy[mask] - hy) / max(reach, 1.0)
            t = np.clip(d, 0.0, 1.0)[:, None].astype(np.float32)
            lit = mix(colour, self.ink.white, light)
            dark = mix(colour, self.ink.black, shadow)
            self.rgb[mask] = lit * (1.0 - t) + dark * t
        self.alpha |= mask

    # --- shapes --------------------------------------------------------------------
    def polygon(self, points: Sequence[Point], colour: Colour, **kw) -> None:
        pts = [(float(x), float(y)) for x, y in points]
        self.paint(self.mask(lambda d: d.polygon(pts, fill=255)), colour, **kw)

    @staticmethod
    def _box(box: Sequence[float]) -> tuple[float, float, float, float] | None:
        """A Pillow box with ordered corners; ``None`` when it has no area."""
        x0, y0, x1, y1 = (float(v) for v in box)
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        if x1 - x0 < 1.0 or y1 - y0 < 1.0:
            return None
        return (x0, y0, x1, y1)

    def ellipse(self, box: Sequence[float], colour: Colour, **kw) -> None:
        b = self._box(box)
        if b is not None:
            self.paint(self.mask(lambda d: d.ellipse(b, fill=255)), colour, **kw)

    def rect(self, box: Sequence[float], colour: Colour, **kw) -> None:
        b = self._box(box)
        if b is not None:
            self.paint(self.mask(lambda d: d.rectangle(b, fill=255)), colour, **kw)

    def line(self, points: Sequence[Point], colour: Colour, width: int = 2) -> None:
        pts = [(float(x), float(y)) for x, y in points]
        self.paint(self.mask(lambda d: d.line(pts, fill=255, width=width, joint="curve")), colour)

    def pieslice(self, box: Sequence[float], start: float, end: float, colour: Colour) -> None:
        b = tuple(float(v) for v in box)
        self.paint(self.mask(lambda d: d.pieslice(b, start, end, fill=255)), colour)

    def ball(self, cx: float, cy: float, r: float, colour: Colour, **kw) -> None:
        """A shaded disc, lit from the upper left."""
        kw.setdefault("highlight", (cx - 0.35 * r, cy - 0.35 * r, 1.6 * r))
        self.ellipse((cx - r, cy - r, cx + r, cy + r), colour, **kw)

    def clip(self, x: float, y: float) -> Point:
        return (min(max(x, 0.0), self.w - 1.0), min(max(y, 0.0), self.h - 1.0))

    # --- output --------------------------------------------------------------------
    def rgb_u8(self) -> np.ndarray:
        return np.clip(np.rint(self.rgb), 0, 255).astype(np.uint8)


# --- geometry helpers ------------------------------------------------------------------------


def rotate(points: Sequence[Point], angle: float, about: Point) -> list[Point]:
    """Rotate ``points`` by ``angle`` radians around ``about``."""
    ca, sa = math.cos(angle), math.sin(angle)
    ox, oy = about
    return [
        (ox + (x - ox) * ca - (y - oy) * sa, oy + (x - ox) * sa + (y - oy) * ca) for x, y in points
    ]


def heart(cx: float, cy: float, size: float, angle: float = 0.0, n: int = 48) -> list[Point]:
    """A heart of about ``size`` px across, centred near ``(cx, cy)``, its point down."""
    pts: list[Point] = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x = 16 * math.sin(t) ** 3
        y = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
        pts.append((cx + x * size / 32, cy + y * size / 32 - size * 0.06))
    return rotate(pts, angle, (cx, cy)) if angle else pts


def star(cx: float, cy: float, r: float, points: int = 5, inner: float = 0.42) -> list[Point]:
    pts: list[Point] = []
    for i in range(points * 2):
        rad = r if i % 2 == 0 else r * inner
        a = -math.pi / 2 + i * math.pi / points
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    return pts


def leaf(
    cx: float, cy: float, length: float, width: float, angle: float, n: int = 24
) -> list[Point]:
    """A pointed leaf along the x axis, rotated by ``angle``."""
    pts: list[Point] = []
    for i in range(n + 1):
        t = i / n
        pts.append((cx - length / 2 + t * length, cy - math.sin(math.pi * t) * width / 2))
    for i in range(n, -1, -1):
        t = i / n
        pts.append((cx - length / 2 + t * length, cy + math.sin(math.pi * t) * width / 2))
    return rotate(pts, angle, (cx, cy))


def egg(cx: float, cy: float, rx: float, ry: float, n: int = 72) -> list[Point]:
    """An egg standing upright: an ellipse narrowed toward the top."""
    pts: list[Point] = []
    for i in range(n):
        t = 2 * math.pi * i / n
        y = -math.cos(t)  # -1 top .. 1 bottom
        taper = 1.0 - 0.18 * (-y)  # narrower above the middle
        x = math.sin(t) * (taper if y < 0 else 1.0)
        pts.append((cx + x * rx, cy + y * ry))
    return pts


def scatter(
    rng: random.Random,
    w: int,
    h: int,
    count: int,
    radius: Callable[[], float],
    *,
    avoid: Sequence[tuple[float, float, float]] = (),
    margin: float = 4.0,
    tries: int = 40,
) -> list[tuple[float, float, float]]:
    """``count`` non-overlapping discs ``(x, y, r)`` inside the box, clear of
    ``avoid``.  Best effort: a crowded box yields fewer."""
    placed: list[tuple[float, float, float]] = list(avoid)
    out: list[tuple[float, float, float]] = []
    for _ in range(count):
        for _attempt in range(tries):
            r = radius()
            if 2 * r + 2 * margin > min(w, h):
                r = (min(w, h) - 2 * margin) / 2
            x = rng.uniform(r + margin, w - r - margin)
            y = rng.uniform(r + margin, h - r - margin)
            if all(math.hypot(x - px, y - py) >= r + pr + margin for px, py, pr in placed):
                placed.append((x, y, r))
                out.append((x, y, r))
                break
    return out


def wide(s: Scene) -> bool:
    """A banner-shaped box: wider than 1.6 times its height."""
    return s.w > 1.6 * s.h


# --- the motifs -----------------------------------------------------------------------------


def _burst(
    s: Scene, rng: random.Random, cx: float, cy: float, r: float, colour: Colour, rays: int = 14
) -> None:
    ink = s.ink
    width = max(2, int(r / 14))
    for i in range(rays):
        a = 2 * math.pi * i / rays + rng.uniform(-0.08, 0.08)
        inner = r * rng.uniform(0.22, 0.34)
        outer = r * rng.uniform(0.82, 1.0)
        p0 = (cx + inner * math.cos(a), cy + inner * math.sin(a))
        p1 = (cx + outer * math.cos(a), cy + outer * math.sin(a))
        s.line([p0, p1], colour, width=width)
        dot = max(2.0, r / 11)
        s.ellipse((p1[0] - dot, p1[1] - dot, p1[0] + dot, p1[1] + dot), colour)
    core = max(3.0, r * 0.1)
    s.ball(cx, cy, core, mix(colour, ink.white, 0.3))


def fireworks(s: Scene, rng: random.Random, colours: Sequence[Colour], clear: Box) -> None:
    """Bursts around a clear box (the caption's place), each as large as the
    room between the box and the edge allows."""
    w, h = s.w, s.h
    cx0, cy0, cx1, cy1 = clear
    if wide(s):
        # three on each side of the caption
        spots = [
            (cx0 * 0.17, h * 0.62),
            (cx0 * 0.5, h * 0.45),
            (cx0 * 0.84, h * 0.6),
            (cx1 + (w - cx1) * 0.16, h * 0.4),
            (cx1 + (w - cx1) * 0.5, h * 0.55),
            (cx1 + (w - cx1) * 0.83, h * 0.38),
        ]
    else:
        spots = [
            (w * 0.22, cy0 * 0.5),
            (w * 0.72, cy0 * 0.42),
            (w * 0.5, cy0 * 0.75) if cy0 > h * 0.3 else (w * 0.88, cy0 * 0.7),
            (w * 0.25, cy1 + (h - cy1) * 0.5),
            (w * 0.75, cy1 + (h - cy1) * 0.55),
        ]
    for i, (x, y) in enumerate(spots):
        x += rng.uniform(-0.03, 0.03) * w
        y += rng.uniform(-0.03, 0.03) * h
        # the largest radius that stays inside the box and out of the caption
        room_edge = min(x, w - x, y, h - y)
        dx = max(cx0 - x, x - cx1, 0.0)
        dy = max(cy0 - y, y - cy1, 0.0)
        room_clear = math.hypot(dx, dy) if dx and dy else max(dx, dy)
        r = max(12.0, min(room_edge, room_clear) * 0.95)
        _burst(s, rng, x, y, r, colours[i % len(colours)], rays=rng.choice((12, 14, 16)))


def motif_new_year(s: Scene, rng: random.Random) -> None:
    ink = s.ink
    w, h = s.w, s.h
    if wide(s):
        clear = (int(w * 0.3), int(h * 0.16), int(w * 0.7), int(h * 0.84))
    else:
        clear = (int(w * 0.1), int(h * 0.36), int(w * 0.9), int(h * 0.64))
    s.caption = (clear, BLACK)
    fireworks(s, rng, (ink.red, ink.blue, ink.gold, ink.green, ink.orange, ink.lavender), clear)


def motif_valentines(s: Scene, rng: random.Random) -> None:
    ink = s.ink
    w, h = s.w, s.h
    big = min(w, h) * (0.56 if not wide(s) else 0.8)
    cx, cy = w / 2, h / 2 + big * 0.02
    s.polygon(heart(cx, cy, big), ink.red, highlight=(cx - big * 0.25, cy - big * 0.3, big * 0.9))
    s.ellipse((cx - big * 0.26, cy - big * 0.3, cx - big * 0.12, cy - big * 0.18), ink.light_pink)
    small = scatter(
        rng,
        w,
        h,
        14 if wide(s) else 9,
        lambda: rng.uniform(0.06, 0.13) * min(w, h) * (1.3 if wide(s) else 1),
        avoid=[(cx, cy, big * 0.52)],
        margin=3.0,
    )
    palette = (ink.pink, ink.red, ink.light_pink, ink.pink)
    for i, (x, y, r) in enumerate(small):
        s.polygon(heart(x, y, 2 * r, rng.uniform(-0.5, 0.5)), palette[i % len(palette)])


def _shamrock(s: Scene, cx: float, cy: float, size: float, colour: Colour, dark: Colour) -> None:
    leaf_size = size * 0.52
    for k in range(3):
        a = -math.pi / 2 + k * 2 * math.pi / 3  # one leaf up, two down
        lx, ly = cx + math.cos(a) * size * 0.2, cy + math.sin(a) * size * 0.2
        pts = heart(lx, ly, leaf_size, a + math.pi / 2)
        s.polygon(
            pts, colour, highlight=(cx - size * 0.2, cy - size * 0.25, size * 0.9), light=0.35
        )
    s.line(
        [(cx, cy + size * 0.1), (cx + size * 0.12, cy + size * 0.55)],
        dark,
        width=max(2, int(size / 16)),
    )


def motif_st_patricks(s: Scene, rng: random.Random) -> None:
    ink = s.ink
    w, h = s.w, s.h
    big = min(w, h) * (0.62 if not wide(s) else 0.85)
    cx, cy = w / 2, h / 2 - big * 0.04
    _shamrock(s, cx, cy, big, ink.green, ink.dark_green)
    small = scatter(
        rng,
        w,
        h,
        12 if wide(s) else 7,
        lambda: rng.uniform(0.07, 0.12) * min(w, h) * (1.3 if wide(s) else 1),
        avoid=[(cx, cy, big * 0.5)],
        margin=3.0,
    )
    for i, (x, y, r) in enumerate(small):
        _shamrock(
            s, x, y, 2 * r, (ink.light_green, ink.green, ink.dark_green)[i % 3], ink.dark_green
        )


def _decorated_egg(
    s: Scene, rng: random.Random, cx: float, cy: float, rx: float, ry: float, style: int
) -> None:
    ink = s.ink
    pastels = (ink.lavender, ink.light_pink, ink.light_green, ink.cream, ink.sky, ink.tan)
    base = pastels[style % len(pastels)]
    shell = s.mask(lambda d: d.polygon(egg(cx, cy, rx, ry), fill=255))
    s.paint(
        shell, base, highlight=(cx - rx * 0.35, cy - ry * 0.45, ry * 1.5), light=0.4, shadow=0.18
    )
    yy = s._yy
    bands = [(-0.55, -0.4), (-0.2, 0.0), (0.25, 0.4)]
    accent = [
        pastels[(style + 2) % 6],
        pastels[(style + 4) % 6],
        ink.pink if style % 2 else ink.green,
    ]
    for (t0, t1), col in zip(bands, accent, strict=True):
        band = shell & (yy >= cy + t0 * ry) & (yy < cy + t1 * ry)
        s.paint(band, col)
    # a zigzag through the middle band and a row of dots below it
    zig_y = cy - 0.1 * ry
    amp, step = ry * 0.06, max(6.0, rx / 4)
    pts = [
        (cx - rx * 1.1 + i * step, zig_y + (amp if i % 2 else -amp))
        for i in range(int(2.4 * rx / step) + 2)
    ]
    zig = s.mask(lambda d: d.line(pts, fill=255, width=max(2, int(ry / 28))))
    s.paint(zig & shell, ink.white if style % 2 else ink.dark_red)
    dot_y = cy + 0.58 * ry
    dot_r = max(2.0, ry * 0.05)
    for i in range(-2, 3):
        dx = cx + i * rx * 0.36
        s.paint(
            shell
            & s.mask(
                lambda d, dx=dx: d.ellipse(
                    (dx - dot_r, dot_y - dot_r, dx + dot_r, dot_y + dot_r), fill=255
                )
            ),
            ink.white if style % 2 == 0 else ink.red,
        )
    dot_y2 = cy - 0.75 * ry
    for i in range(-1, 2):
        dx = cx + i * rx * 0.3
        s.paint(
            shell
            & s.mask(
                lambda d, dx=dx: d.ellipse(
                    (dx - dot_r, dot_y2 - dot_r, dx + dot_r, dot_y2 + dot_r), fill=255
                )
            ),
            ink.red if style % 2 == 0 else ink.blue,
        )


def _grass(s: Scene, rng: random.Random, x0: float, x1: float, y: float, height: float) -> None:
    ink = s.ink
    n = max(4, int((x1 - x0) / 9))
    for i in range(n):
        x = x0 + (x1 - x0) * (i + 0.5) / n + rng.uniform(-3, 3)
        hgt = height * rng.uniform(0.5, 1.0)
        lean = rng.uniform(-0.3, 0.3) * hgt
        s.polygon(
            [(x - 3, y), (x + 3, y), (x + lean, y - hgt)], ink.green if i % 2 else ink.dark_green
        )


def motif_easter(s: Scene, rng: random.Random) -> None:
    w, h = s.w, s.h
    if wide(s):
        ry = h * 0.4
        rx = ry * 0.74
        xs = [w * 0.25, w * 0.5, w * 0.75]
        cy = h * 0.5
        for i, cx in enumerate(xs):
            _decorated_egg(s, rng, cx, cy, rx, ry, i)
        _grass(s, rng, w * 0.05, w * 0.95, cy + ry * 0.98, h * 0.12)
    else:
        ry = min(h * 0.4, w * 0.42)
        rx = ry * 0.76
        cx, cy = w / 2, h * 0.48
        _decorated_egg(s, rng, cx, cy, rx, ry, 0)
        _grass(s, rng, cx - rx * 1.5, cx + rx * 1.5, cy + ry * 0.98, ry * 0.22)


def motif_memorial_day(s: Scene, rng: random.Random) -> None:
    ink = s.ink
    w, h = s.w, s.h
    # a flag on a pole, planted left of centre, waving to the right
    fh = h * 0.58 if not wide(s) else h * 0.74
    fw = min(fh * 1.9, w * 0.72)
    x0 = (w - fw) / 2 + fw * 0.04
    y0 = (h - fh) / 2 - (fh * 0.12 if not wide(s) else 0)
    pole_x = x0 - 6
    stripe = fh / 13
    for i in range(13):
        s.rect(
            (x0, y0 + i * stripe, x0 + fw, y0 + (i + 1) * stripe),
            ink.red if i % 2 == 0 else ink.white,
        )
    cw, ch = fw * 0.4, stripe * 7
    s.rect((x0, y0, x0 + cw, y0 + ch), ink.blue)
    rows, cols = 5, 6
    sr = min(cw / cols, ch / rows) * 0.26
    for r in range(rows):
        for c in range(cols):
            sx = x0 + cw * (c + 0.5) / cols
            sy = y0 + ch * (r + 0.5) / rows
            s.polygon(star(sx, sy, sr), ink.white)
    # wave: shift each column of the flag up and down with a gentle sine
    amp = fh * 0.05
    xs = range(int(x0), int(x0 + fw) + 1)
    region = (slice(None), slice(int(x0), int(x0 + fw) + 1))
    rgb = s.rgb[region].copy()
    alpha = s.alpha[region].copy()
    for j, x in enumerate(xs):
        if j >= rgb.shape[1]:
            break
        shift = int(round(amp * math.sin(2 * math.pi * (x - x0) / fw * 1.5) * (x - x0) / fw))
        s.rgb[:, x] = np.roll(rgb[:, j], shift, axis=0)
        s.alpha[:, x] = np.roll(alpha[:, j], shift)
    s.rect((pole_x - 3, y0 - fh * 0.08, pole_x + 3, min(h - 2.0, y0 + fh * 1.3)), ink.brown)
    s.ball(pole_x, y0 - fh * 0.1, 7, ink.gold)
    if wide(s):
        for sx, sy, sr, col in (
            (x0 * 0.5, h * 0.35, fh * 0.14, ink.blue),
            (x0 * 0.25, h * 0.7, fh * 0.1, ink.red),
            (x0 + fw + (w - x0 - fw) * 0.5, h * 0.35, fh * 0.14, ink.red),
            (x0 + fw + (w - x0 - fw) * 0.78, h * 0.7, fh * 0.1, ink.blue),
        ):
            s.polygon(star(sx, sy, sr), col)


def motif_independence_day(s: Scene, rng: random.Random) -> None:
    ink = s.ink
    w, h = s.w, s.h
    big = min(w, h) * (0.3 if not wide(s) else 0.42)
    cx, cy = w / 2, h / 2
    s.polygon(
        star(cx, cy, big),
        ink.blue,
        highlight=(cx - big * 0.3, cy - big * 0.4, big * 1.4),
        light=0.3,
    )
    s.polygon(star(cx, cy, big * 0.55), ink.red)
    s.polygon(star(cx, cy, big * 0.22), ink.white)
    spots = scatter(
        rng,
        w,
        h,
        12 if wide(s) else 8,
        lambda: rng.uniform(0.07, 0.16) * min(w, h) * (1.3 if wide(s) else 1),
        avoid=[(cx, cy, big * 1.05)],
        margin=3.0,
    )
    for i, (x, y, r) in enumerate(spots):
        if i % 3 == 2:
            _burst(s, rng, x, y, r * 1.1, (ink.red, ink.blue)[i % 2], rays=10)
        else:
            s.polygon(star(x, y, r, inner=0.45), (ink.red, ink.blue)[i % 2])


def motif_labor_day(s: Scene, rng: random.Random) -> None:
    """A ribbon banner (the layout letters "LABOR DAY" on it) under a gear badge."""
    ink = s.ink
    w, h = s.w, s.h
    bw = w * (0.96 if not wide(s) else 0.7)
    bh = min(h * 0.24, bw * 0.3)
    gr = min(h * 0.17, bh * 1.1)  # the gear badge above the ribbon
    group_h = gr * 2.1 + bh * 1.45
    top = (h - group_h) / 2
    gcx, gcy = w / 2, top + gr * 1.05
    cx, cy = w / 2, top + gr * 2.1 + bh / 2 + bh * 0.1
    x0, x1 = cx - bw * 0.34, cx + bw * 0.34
    y0, y1 = cy - bh / 2, cy + bh / 2
    tail_w, drop = bw * 0.16, bh * 0.4
    # tails behind, notched, a shade darker where they fold under
    for sign in (-1, 1):
        ex = x0 if sign < 0 else x1
        far = ex - sign * tail_w
        notch = far + sign * tail_w * 0.35
        pts = [
            (ex, y0 + drop),
            (far, y0 + drop),
            (notch, y0 + drop + bh / 2),
            (far, y1 + drop),
            (ex, y1 + drop),
        ]
        s.polygon(pts, ink.red)
        s.polygon([(ex, y1), (ex, y1 + drop), (ex - sign * bh * 0.3, y1 + drop)], ink.dark_red)
    s.rect((x0, y0, x1, y1), ink.blue)
    s.rect((x0, y0, x1, y0 + bh * 0.08), mix(ink.blue, ink.white, 0.35))
    s.rect((x0, y1 - bh * 0.08, x1, y1), ink.navy)
    pad = bh * 0.16
    s.caption = ((int(x0 + pad), int(y0 + pad), int(x1 - pad), int(y1 - pad)), WHITE)
    # the gear badge
    teeth = 10
    pts: list[Point] = []
    for i in range(teeth * 2):
        rad = gr if i % 2 == 0 else gr * 0.78
        a = i * math.pi / teeth
        a2 = a + math.pi / teeth * 0.5
        pts.append((gcx + rad * math.cos(a), gcy + rad * math.sin(a)))
        pts.append((gcx + rad * math.cos(a2), gcy + rad * math.sin(a2)))
    s.polygon(pts, ink.gold)
    s.ball(gcx, gcy, gr * 0.58, ink.yellow)
    s.polygon(star(gcx, gcy, gr * 0.36), ink.navy)
    if wide(s):
        for sign in (-1, 1):
            sx = cx + sign * bw * 0.6
            s.polygon(star(sx, cy, bh * 0.45), ink.red)
            s.polygon(star(sx + sign * bh * 0.9, cy - bh * 0.5, bh * 0.25), ink.blue)


def _bat(s: Scene, cx: float, cy: float, span: float) -> None:
    ink = s.ink
    pts: list[Point] = [(cx, cy + span * 0.08)]
    for sign in (1, -1):
        wing = [
            (cx + sign * span * 0.08, cy - span * 0.1),
            (cx + sign * span * 0.3, cy - span * 0.22),
            (cx + sign * span * 0.5, cy - span * 0.05),
            (cx + sign * span * 0.4, cy + span * 0.1),
            (cx + sign * span * 0.28, cy + span * 0.02),
            (cx + sign * span * 0.16, cy + span * 0.14),
        ]
        pts = pts + wing if sign == 1 else pts + wing[::-1]
    s.polygon(pts, ink.black)


def _pumpkin(s: Scene, cx: float, cy: float, r: float) -> None:
    ink = s.ink
    body_w, body_h = r * 1.15, r
    # lobes: overlapping ellipses, outer ones a shade darker, seams between
    lobes = (
        (-0.62, 0.78, ink.dark_orange),
        (0.62, 0.78, ink.dark_orange),
        (-0.3, 0.92, mix(ink.orange, ink.black, 0.1)),
        (0.3, 0.92, mix(ink.orange, ink.black, 0.1)),
        (0.0, 1.0, ink.orange),
    )
    for dx, scale, col in lobes:
        lx = cx + dx * body_w
        lw, lh = body_w * 0.45 * scale, body_h * scale
        s.ellipse(
            (lx - lw, cy - lh, lx + lw, cy + lh),
            col,
            highlight=(cx - body_w * 0.3, cy - body_h * 0.6, body_h * 2.2),
            light=0.18,
            shadow=0.18,
        )
    # stem
    sw, sh = r * 0.14, r * 0.3
    s.polygon(
        [
            (cx - sw, cy - body_h * 0.92),
            (cx + sw, cy - body_h * 0.92),
            (cx + sw * 1.4, cy - body_h - sh),
            (cx - sw * 0.4, cy - body_h - sh * 1.1),
        ],
        ink.dark_green,
    )
    # face: flat palette black with a yellow glow inside each cut
    eye_w, eye_h = r * 0.3, r * 0.26
    ey = cy - body_h * 0.18
    for sign in (-1, 1):
        ex = cx + sign * body_w * 0.38
        s.polygon(
            [
                (ex - eye_w / 2, ey + eye_h / 2),
                (ex + eye_w / 2, ey + eye_h / 2),
                (ex, ey - eye_h / 2),
            ],
            ink.black,
        )
        s.polygon(
            [(ex - eye_w / 4, ey + eye_h / 2 - 2), (ex + eye_w / 4, ey + eye_h / 2 - 2), (ex, ey)],
            ink.yellow,
        )
    ny = cy + body_h * 0.12
    s.polygon(
        [(cx - r * 0.1, ny + r * 0.1), (cx + r * 0.1, ny + r * 0.1), (cx, ny - r * 0.1)], ink.black
    )
    # a jagged grin
    my = cy + body_h * 0.42
    mw = body_w * 0.9
    top = [(cx - mw / 2, my - r * 0.12)]
    for i in range(1, 6):
        x = cx - mw / 2 + mw * i / 6
        top.append((x, my - r * 0.12 + (r * 0.1 if i % 2 else 0)))
    top.append((cx + mw / 2, my - r * 0.12))
    bottom = [
        (cx + mw * 0.35, my + r * 0.22),
        (cx + mw * 0.1, my + r * 0.3),
        (cx - mw * 0.1, my + r * 0.3),
        (cx - mw * 0.35, my + r * 0.22),
    ]
    s.polygon(top + bottom, ink.black)
    s.rect((cx + mw * 0.1, my - r * 0.02, cx + mw * 0.22, my + r * 0.1), ink.yellow)
    s.rect((cx - mw * 0.25, my + r * 0.12, cx - mw * 0.14, my + r * 0.2), ink.yellow)


def motif_halloween(s: Scene, rng: random.Random) -> None:
    w, h = s.w, s.h
    if wide(s):
        r = h * 0.36
        _pumpkin(s, w / 2, h * 0.56, r)
        _pumpkin(s, w * 0.2, h * 0.64, r * 0.62)
        _pumpkin(s, w * 0.8, h * 0.64, r * 0.62)
        for x, y in (
            (w * 0.08, h * 0.2),
            (w * 0.36, h * 0.12),
            (w * 0.66, h * 0.14),
            (w * 0.93, h * 0.24),
        ):
            _bat(s, x, y, h * 0.3)
    else:
        r = min(w * 0.4, h * 0.38)
        _pumpkin(s, w / 2, h * 0.56, r)
        for x, y in ((w * 0.14, h * 0.14), (w * 0.84, h * 0.1)):
            _bat(s, x, y, min(w, h) * 0.3)


def _pie(s: Scene, cx: float, cy: float, width: float) -> None:
    ink = s.ink
    dish_h = width * 0.16
    crust_h = width * 0.28
    # the dish, then the crust dome with a scalloped rim and a cut wedge
    s.polygon(
        [
            (cx - width / 2, cy),
            (cx + width / 2, cy),
            (cx + width * 0.42, cy + dish_h),
            (cx - width * 0.42, cy + dish_h),
        ],
        ink.blue,
        highlight=(cx - width * 0.3, cy, width * 0.9),
        light=0.3,
        shadow=0.2,
    )
    s.rect((cx - width / 2, cy - width * 0.02, cx + width / 2, cy + width * 0.02), ink.navy)
    dome = s.mask(
        lambda d: d.ellipse(
            (cx - width * 0.46, cy - crust_h, cx + width * 0.46, cy + crust_h * 0.6), fill=255
        )
    )
    dome &= s._yy <= cy
    s.paint(
        dome,
        ink.gold,
        highlight=(cx - width * 0.15, cy - crust_h * 0.9, width * 0.7),
        light=0.3,
        shadow=0.3,
    )
    # lattice: two sets of diagonal strips over the dome
    yy, xx = s._yy, s._xx
    period = max(8, int(width / 9))
    lattice = (((xx + yy) // period) % 2 == 0) & (((xx - yy) // period) % 2 == 0)
    s.paint(dome & lattice, ink.brown)
    rim_r = max(3.0, width * 0.035)
    n = int(width / (rim_r * 1.8))
    for i in range(n + 1):
        x = cx - width * 0.46 + (width * 0.92) * i / n
        s.ellipse((x - rim_r, cy - rim_r * 1.2, x + rim_r, cy + rim_r * 0.8), ink.brown)
    # the wedge, showing the filling
    wedge = s.mask(
        lambda d: d.polygon(
            [
                (cx + width * 0.05, cy - 1),
                (cx + width * 0.3, cy - crust_h * 0.75),
                (cx + width * 0.42, cy - 1),
            ],
            fill=255,
        )
    )
    s.paint(wedge & dome, ink.red)
    # steam
    for dx in (-0.12, 0.08):
        x = cx + dx * width
        top = cy - crust_h * 1.08
        pts = [
            (x + (width * 0.02 if i % 2 else -width * 0.02), top - i * width * 0.06)
            for i in range(4)
        ]
        s.line(pts, ink.grey, width=max(2, int(width / 50)))


def _autumn_leaf(
    s: Scene, rng: random.Random, x: float, y: float, size: float, colour: Colour
) -> None:
    ink = s.ink
    angle = rng.uniform(0, math.pi)
    s.polygon(
        leaf(x, y, size, size * 0.5, angle),
        colour,
        highlight=(x - size * 0.2, y - size * 0.2, size * 0.9),
        light=0.25,
        shadow=0.15,
    )
    tip = (x + math.cos(angle) * size / 2, y + math.sin(angle) * size / 2)
    base = (x - math.cos(angle) * size / 2, y - math.sin(angle) * size / 2)
    s.line([base, tip], ink.brown, width=max(1, int(size / 22)))


def motif_thanksgiving(s: Scene, rng: random.Random) -> None:
    ink = s.ink
    w, h = s.w, s.h
    pie_w = min(w * 0.66, h * 0.95) if not wide(s) else h * 1.3
    pcx, pcy = w / 2, h * 0.6 if not wide(s) else h * 0.62
    _pie(s, pcx, pcy, pie_w)
    spots = scatter(
        rng,
        w,
        h,
        10 if wide(s) else 7,
        lambda: rng.uniform(0.07, 0.11) * min(w, h) * (1.4 if wide(s) else 1),
        avoid=[(pcx, pcy - pie_w * 0.1, pie_w * 0.55)],
        margin=2.0,
    )
    colours = (ink.orange, ink.red, ink.gold, ink.brown, ink.dark_orange)
    for i, (x, y, r) in enumerate(spots):
        _autumn_leaf(s, rng, x, y, 2 * r, colours[i % len(colours)])


def _gift(s: Scene, x: float, y: float, size: float, colour: Colour, ribbon: Colour) -> None:
    s.rect((x - size / 2, y - size / 2, x + size / 2, y + size / 2), colour)
    s.rect((x - size * 0.09, y - size / 2, x + size * 0.09, y + size / 2), ribbon)
    s.rect((x - size / 2, y - size * 0.09, x + size / 2, y + size * 0.09), ribbon)


def _tree(s: Scene, rng: random.Random, cx: float, top: float, height: float) -> None:
    ink = s.ink
    tiers = 3
    trunk_h = height * 0.12
    tree_h = height - trunk_h
    s.rect(
        (cx - height * 0.05, top + tree_h - 2, cx + height * 0.05, top + tree_h + trunk_h),
        ink.brown,
    )
    baubles: list[tuple[float, float]] = []
    for i in range(tiers):
        t0 = top + tree_h * (0.12 + 0.3 * i)
        t1 = top + tree_h * (0.42 + 0.3 * i) if i < tiers - 1 else top + tree_h
        half = tree_h * (0.22 + 0.11 * i)
        s.polygon(
            [(cx, t0 - (tree_h * 0.14 if i else 0)), (cx + half, t1), (cx - half, t1)],
            ink.green,
            highlight=(cx - half * 0.6, t0, tree_h * 0.6),
            light=0.18,
            shadow=0.3,
        )
        for k in range(2 + i):
            u = (k + 0.5) / (2 + i)
            bx = cx - half * 0.8 + 1.6 * half * u
            by = t1 - tree_h * 0.05 - abs(u - 0.5) * tree_h * 0.12
            baubles.append((bx, by))
    r = height * 0.03
    for i, (bx, by) in enumerate(baubles):
        s.ball(bx, by, r, ink.red if i % 2 == 0 else ink.yellow)
    s.polygon(star(cx, top + tree_h * 0.03, height * 0.07), ink.yellow)


def motif_christmas(s: Scene, rng: random.Random) -> None:
    ink = s.ink
    w, h = s.w, s.h
    if wide(s):
        height = h * 0.88
        _tree(s, rng, w / 2, h * 0.06, height)
        _gift(s, w * 0.33, h * 0.82, h * 0.2, ink.red, ink.yellow)
        _gift(s, w * 0.66, h * 0.84, h * 0.16, ink.blue, ink.yellow)
        _tree(s, rng, w * 0.16, h * 0.3, h * 0.64)
        _tree(s, rng, w * 0.84, h * 0.3, h * 0.64)
    else:
        height = min(h * 0.9, w * 1.1)
        _tree(s, rng, w / 2, (h - height) / 2, height)
        g = height * 0.12
        _gift(s, w / 2 - height * 0.33, (h + height) / 2 - g / 2, g, ink.red, ink.yellow)
        _gift(s, w / 2 + height * 0.34, (h + height) / 2 - g * 0.4, g * 0.8, ink.blue, ink.yellow)
    # snow
    for x, y, r in scatter(rng, w, h, 10, lambda: 2.5, margin=1.0):
        if not s.alpha[int(y), int(x)]:
            s.ball(x, y, r, ink.sky, highlight=None)


def _balloon(s: Scene, cx: float, cy: float, r: float, colour: Colour, string_to: Point) -> None:
    ink = s.ink
    s.ellipse(
        (cx - r, cy - r * 1.2, cx + r, cy + r * 1.2),
        colour,
        highlight=(cx - r * 0.35, cy - r * 0.55, r * 2.0),
        light=0.5,
        shadow=0.2,
    )
    knot_y = cy + r * 1.2
    s.polygon(
        [(cx, knot_y - 2), (cx - r * 0.16, knot_y + r * 0.18), (cx + r * 0.16, knot_y + r * 0.18)],
        colour,
    )
    mid = ((cx + string_to[0]) / 2 + r * 0.25, (knot_y + string_to[1]) / 2)
    s.line([(cx, knot_y + r * 0.18), mid, string_to], ink.grey, width=1)


def _cake(s: Scene, cx: float, base_y: float, width: float) -> None:
    ink = s.ink
    plate_h = width * 0.08
    s.ellipse(
        (cx - width * 0.6, base_y - plate_h, cx + width * 0.6, base_y + plate_h), ink.lavender
    )
    tiers = (
        (width, width * 0.26, mix(ink.red, ink.white, 0.3), ink.white),
        (width * 0.68, width * 0.22, mix(ink.yellow, ink.white, 0.4), ink.red),
    )
    y = base_y
    for tw, th, body, icing in tiers:
        s.rect(
            (cx - tw / 2, y - th, cx + tw / 2, y),
            body,
            highlight=(cx - tw * 0.4, y - th, tw * 1.2),
            light=0.25,
            shadow=0.12,
        )
        drip = th * 0.3
        s.rect((cx - tw / 2, y - th, cx + tw / 2, y - th + drip * 0.5), icing)
        n = max(3, int(tw / (drip * 1.3)))
        for i in range(n + 1):
            dx = cx - tw / 2 + tw * i / n
            s.ellipse((dx - drip * 0.5, y - th + drip * 0.1, dx + drip * 0.5, y - th + drip), icing)
        y -= th
    # candles with flames
    top_w = tiers[1][0]
    for i in range(3):
        x = cx - top_w * 0.3 + top_w * 0.3 * i
        ch, cw = width * 0.14, max(3.0, width * 0.03)
        s.rect((x - cw, y - ch, x + cw, y), ink.red if i % 2 == 0 else ink.blue)
        s.rect((x - cw, y - ch * 0.7, x + cw, y - ch * 0.55), ink.white)
        s.rect((x - cw, y - ch * 0.3, x + cw, y - ch * 0.15), ink.white)
        fr = cw * 1.6
        s.polygon(
            [
                (x, y - ch - fr * 2.6),
                (x + fr, y - ch - fr * 0.8),
                (x, y - ch + fr * 0.2),
                (x - fr, y - ch - fr * 0.8),
            ],
            ink.yellow,
        )
        s.ellipse((x - fr * 0.45, y - ch - fr * 1.2, x + fr * 0.45, y - ch - fr * 0.1), ink.orange)


def motif_birthday(s: Scene, rng: random.Random) -> None:
    ink = s.ink
    w, h = s.w, s.h
    colours = (ink.red, ink.blue, ink.yellow, ink.green, ink.pink)
    if wide(s):
        cake_w = h * 0.7
        cx, base = w / 2, h * 0.9
        _cake(s, cx, base, cake_w)
        r = h * 0.17
        for i, (bx, by) in enumerate(
            ((w * 0.14, h * 0.3), (w * 0.26, h * 0.5), (w * 0.74, h * 0.5), (w * 0.86, h * 0.3))
        ):
            _balloon(
                s,
                bx,
                by,
                r,
                colours[i % len(colours)],
                (bx + (12 if bx < w / 2 else -12), h * 0.95),
            )
    else:
        cake_w = min(w * 0.7, h * 0.56)
        cx, base = w / 2, h * 0.92
        _cake(s, cx, base, cake_w)
        r = min(w, h) * 0.12
        for i, (bx, by) in enumerate(
            ((w * 0.2, h * 0.18), (w * 0.82, h * 0.22), (w * 0.66, h * 0.1))
        ):
            _balloon(
                s,
                bx,
                by,
                r,
                colours[i % len(colours)],
                (cx + (-cake_w * 0.5 if bx < w / 2 else cake_w * 0.5), base - cake_w * 0.45),
            )


def motif_custom(s: Scene, rng: random.Random) -> None:
    ink = s.ink
    w, h = s.w, s.h
    colours = (ink.red, ink.blue, ink.yellow, ink.green, ink.pink, ink.lavender, ink.black)
    # streamers first, confetti over them
    amp = min(w, h) * 0.08
    for i in range(3 if not wide(s) else 5):
        x = rng.uniform(w * 0.1, w * 0.9)
        pts = [(x + math.sin(k * 0.55 + i) * amp, h * 0.04 + k * h * 0.92 / 16) for k in range(17)]
        s.line(
            [s.clip(px, py) for px, py in pts],
            colours[(i * 2) % len(colours)],
            width=max(2, int(min(w, h) / 70)),
        )
    unit = min(w, h) / 24
    count = int(w * h / (unit * unit * 6))
    for _i in range(count):
        x, y = rng.uniform(unit, w - unit), rng.uniform(unit, h - unit)
        size = unit * rng.uniform(0.6, 1.3)
        colour = colours[rng.randrange(len(colours))]
        kind = rng.randrange(3)
        if kind == 0:
            s.ellipse((x - size / 2, y - size / 2, x + size / 2, y + size / 2), colour)
        else:
            pts = [
                (x - size / 2, y - size / 4),
                (x + size / 2, y - size / 4),
                (x + size / 2, y + size / 4),
                (x - size / 2, y + size / 4),
            ]
            s.polygon(rotate(pts, rng.uniform(0, math.pi), (x, y)), colour)


MOTIFS: dict[str, Callable[[Scene, random.Random], None]] = {
    "new_year": motif_new_year,
    "valentines": motif_valentines,
    "st_patricks": motif_st_patricks,
    "easter": motif_easter,
    "memorial_day": motif_memorial_day,
    "independence_day": motif_independence_day,
    "labor_day": motif_labor_day,
    "halloween": motif_halloween,
    "thanksgiving": motif_thanksgiving,
    "christmas": motif_christmas,
    "birthday": motif_birthday,
    "custom": motif_custom,
}


def paint_motif(key: str, width: int, height: int, palette: Palette = MEASURED_DEFAULT) -> Scene:
    """The undithered RGB scene for ``key`` (for tests and the design loop)."""
    if key not in MOTIFS:
        raise ValueError(f"unknown motif {key!r}; one of " + ", ".join(MOTIFS))
    s = Scene(width, height, Ink(palette))
    MOTIFS[key](s, random.Random(f"{key}:{width}x{height}"))
    return s


def render_motif(
    key: str,
    width: int,
    height: int,
    palette: Palette = MEASURED_DEFAULT,
    *,
    method: str = DITHER_METHOD,
) -> Motif:
    """Paint motif ``key`` at ``width`` x ``height`` and dither it against
    ``palette`` with the real engine.  Deterministic."""
    from inkframe.render.dither import dither  # the native engine; lazy like render_to_frame

    s = paint_motif(key, width, height, palette)
    rgb = s.rgb_u8()
    indices = dither(
        rgb, palette, method, serpentine=True, strength=DITHER_STRENGTH, distance="lab"
    )
    # pixels painted in an exact palette colour stay that colour: no error
    # diffused in from a neighbouring tone may speckle a flat fill or an edge
    for index, colour in enumerate(palette.rgb):
        flat = np.all(rgb == np.array(colour, dtype=np.uint8), axis=-1)
        indices[flat] = index
    caption_box, caption_fill = s.caption if s.caption else (None, BLACK)
    return Motif(indices, s.alpha.copy(), caption_box, caption_fill)
