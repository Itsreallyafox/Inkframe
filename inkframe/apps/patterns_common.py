"""Shared machinery for the Patterns families (``inkframe.apps.patterns_*``).

* **Moods and schemes**: which of the six colours a composition may use
  (``palette_mood``) and which colour pairs actually read on the panel.  The
  measured panel is not the nominal one: blue is dark, red muted, white a
  little grey, so yellow on white and blue on black are avoided for detail.
* **Plates**: ``mount`` decides between a deliberate full bleed and a print on
  a white mount (24 px margins, an optional keyline, a caption strip at the
  bottom).  Families may snap ``Plate.box`` to their own grid; the app draws
  the keyline and the caption around whatever box the family settled on.
* **Index images**: families composite overlapping shapes as ``(400, 600)``
  palette-index arrays (shapes rasterised by Pillow without anti-aliasing into
  boolean masks), then ``blit`` the result onto the ``Canvas``.  Everything
  stays in exact palette colours by construction.
* **Perlin**: a small vectorised gradient-noise field (numpy only, no scipy).

Every random decision goes through the ``numpy.random.Generator`` handed in by
the app, so one seed always reproduces one composition.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from inkframe.apps.draw import Box, Canvas
from inkframe.display.palette import (
    BLACK,
    BLUE,
    GREEN,
    NOMINAL,
    PANEL_HEIGHT,
    PANEL_WIDTH,
    RED,
    WHITE,
    YELLOW,
)

W, H = PANEL_WIDTH, PANEL_HEIGHT
ACCENTS: tuple[int, ...] = (YELLOW, RED, BLUE, GREEN)
MOODS: tuple[str, ...] = ("full", "duotone", "warm", "cool")

_MOOD_SETS: dict[str, frozenset[int]] = {
    "full": frozenset({BLACK, WHITE, YELLOW, RED, BLUE, GREEN}),
    "warm": frozenset({BLACK, WHITE, YELLOW, RED}),
    "cool": frozenset({BLACK, WHITE, BLUE, GREEN}),
}

#: contrast grades: LINE = fine for strokes and dots, FILL = large shapes only
LINE, FILL, POOR = 2, 1, 0

# judged against the *measured* panel colours (blue and black are both dark,
# yellow and white are both light, red and green sit in the middle)
_PAIR: dict[tuple[int, int], int] = {
    (BLACK, WHITE): LINE,
    (BLACK, YELLOW): LINE,
    (BLACK, RED): FILL,
    (BLACK, BLUE): POOR,
    (BLACK, GREEN): FILL,
    (WHITE, YELLOW): FILL,
    (WHITE, RED): LINE,
    (WHITE, BLUE): LINE,
    (WHITE, GREEN): LINE,
    (YELLOW, RED): LINE,
    (YELLOW, BLUE): LINE,
    (YELLOW, GREEN): LINE,
    (RED, BLUE): FILL,
    (RED, GREEN): FILL,
    (BLUE, GREEN): FILL,
}


def contrast(a: int, b: int) -> int:
    """How well colour ``a`` reads against ``b`` on the panel (LINE/FILL/POOR)."""
    if a == b:
        return POOR
    return _PAIR[(min(a, b), max(a, b))]


def mood_colours(mood: str) -> frozenset[int]:
    """Every colour a mood may ever use (duotone: black, white and all accents,
    of which one composition uses only one accent)."""
    return _MOOD_SETS.get(mood, _MOOD_SETS["full"])


@dataclass(frozen=True)
class Scheme:
    """The colours of one composition."""

    #: the field colour the figure sits on
    ground: int
    #: figure colours by role; ``inks[0]`` is the dominant one
    inks: tuple[int, ...]
    #: every colour this composition may use (ground, inks, paper)
    allowed: frozenset[int]

    def ink(self, i: int) -> int:
        return self.inks[i % len(self.inks)]


def _choice(rng: np.random.Generator, items: list[int] | tuple[int, ...]) -> int:
    return items[int(rng.integers(len(items)))]


def pick_scheme(
    rng: np.random.Generator,
    mood: str,
    *,
    grounds: Mapping[int, float],
    n: int = 3,
    need: int = LINE,
    lead_black: float = 0.5,
    distinct: int = FILL,
) -> Scheme:
    """Choose a ground and up to ``n`` inks that read against it.

    ``grounds`` weights the field colours the family wants (filtered by the
    mood); ``need`` is the contrast every ink must reach against the ground;
    ``distinct`` the contrast every ink must reach against the inks before it
    (so black and blue never share a composition as two "different" inks);
    ``lead_black`` is the chance that black is the dominant ink.  ``duotone``
    is always black plus one accent on white.
    """
    if mood == "duotone":
        eligible = [c for c in ACCENTS if contrast(c, WHITE) >= need] or [RED]
        accent = _choice(rng, eligible)
        allowed = frozenset({WHITE, BLACK, accent})
        ground = WHITE
    else:
        allowed = mood_colours(mood)
        cands = [(g, float(w)) for g, w in grounds.items() if g in allowed and w > 0]
        if not cands:
            cands = [(WHITE, 1.0)]
        weights = np.array([w for _, w in cands], dtype=float)
        ground = cands[int(rng.choice(len(cands), p=weights / weights.sum()))][0]
    inks = [c for c in sorted(allowed) if c != ground and contrast(c, ground) >= need]
    if not inks:
        inks = [c for c in sorted(allowed) if c != ground and contrast(c, ground) >= FILL]
    if not inks:
        inks = [BLACK if ground != BLACK else WHITE]
    others = [c for c in inks if c != BLACK]
    if BLACK in inks and (not others or rng.random() < lead_black):
        lead = BLACK
    else:
        lead = _choice(rng, others)
    rest = [c for c in inks if c != lead]
    rest = [rest[int(i)] for i in rng.permutation(len(rest))]
    chosen = [lead]
    for c in rest:
        # duotone is black plus its one accent by definition, even a dark blue
        if mood == "duotone" or all(contrast(c, p) >= distinct for p in chosen):
            chosen.append(c)
    return Scheme(ground, tuple(chosen[: max(1, n)]), allowed)


def weighted(rng: np.random.Generator, options: Mapping[Any, float]) -> Any:
    """One key of ``options`` drawn with the given weights."""
    keys = list(options)
    p = np.array([options[k] for k in keys], dtype=float)
    return keys[int(rng.choice(len(keys), p=p / p.sum()))]


# --- plates ------------------------------------------------------------------------

MARGIN = 24
CAPTION_BAND = 52


@dataclass
class Plate:
    """Where a composition sits on the card, and what the caption says."""

    #: half-open ``(x0, y0, x1, y1)`` of the composition
    box: Box
    bleed: bool = False
    #: room is reserved under ``box`` for a caption
    caption: bool = False
    #: draw a thin keyline around ``box``
    frame: bool = False
    title: str = ""
    note: str = ""
    #: caption baseline: fixed on the mount, so it never floats when a family
    #: snaps ``box`` smaller than the plate
    caption_y: int = H - CAPTION_BAND + 28


def mount(
    rng: np.random.Generator,
    canvas: Canvas,
    settings: Mapping[str, Any],
    *,
    bleed_p: float,
    frame_p: float = 0.3,
) -> Plate:
    """Full bleed (probability ``bleed_p``) or a print on a white mount."""
    bleed = rng.random() < bleed_p
    framed = rng.random() < frame_p  # drawn either way so later draws never shift
    if bleed:
        return Plate((0, 0, W, H), bleed=True)
    caption = bool(settings.get("caption", True))
    bottom = CAPTION_BAND if caption else MARGIN
    fill_box(canvas, (0, 0, W, H), WHITE)
    return Plate((MARGIN, MARGIN, W - MARGIN, H - bottom), caption=caption, frame=framed)


def fill_box(canvas: Canvas, box: Box, colour: int) -> None:
    """Fill a half-open box."""
    x0, y0, x1, y1 = box
    if x1 > x0 and y1 > y0:
        canvas.rect((x0, y0, x1 - 1, y1 - 1), fill=colour)


def centre_grid(box: Box, cell: int, nx: int, ny: int) -> Box:
    """A ``nx * cell`` by ``ny * cell`` box centred in ``box``."""
    x0, y0, x1, y1 = box
    gx = x0 + ((x1 - x0) - nx * cell) // 2
    gy = y0 + ((y1 - y0) - ny * cell) // 2
    return (gx, gy, gx + nx * cell, gy + ny * cell)


def best_cell(box: Box, target: int, spread: int = 5) -> tuple[int, int, int]:
    """Cell size near ``target`` that tiles ``box`` with the least leftover.

    Returns ``(cell, nx, ny)``."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    best: tuple[int, int, int, int, int] | None = None
    for s in range(max(8, target - spread), target + spread + 1):
        nx, ny = w // s, h // s
        if nx < 1 or ny < 1:
            continue
        score = (w - nx * s) + (h - ny * s)
        key = (score, abs(s - target), s, nx, ny)
        if best is None or key < best:
            best = key
    if best is None:
        return target, 1, 1
    return best[2], best[3], best[4]


# --- index images and masks -----------------------------------------------------------

_RGB_LUT = NOMINAL.as_array()


def frame_of(canvas: Canvas) -> np.ndarray:
    """The canvas as a writable ``(H, W)`` palette-index array."""
    return canvas.to_frame().copy()


def blit(canvas: Canvas, idx: np.ndarray) -> None:
    """Write a full-size palette-index array onto the canvas."""
    canvas.paste(Image.fromarray(_RGB_LUT[idx], "RGB"), (0, 0))


def box_mask(box: Box) -> np.ndarray:
    m = np.zeros((H, W), dtype=bool)
    x0, y0, x1, y1 = (max(0, box[0]), max(0, box[1]), min(W, box[2]), min(H, box[3]))
    if x1 > x0 and y1 > y0:
        m[y0:y1, x0:x1] = True
    return m


def _surface() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("L", (W, H), 0)
    return img, ImageDraw.Draw(img)


def _done(img: Image.Image) -> np.ndarray:
    return np.asarray(img) > 0


def m_disc(cx: float, cy: float, r: float) -> np.ndarray:
    img, d = _surface()
    if r > 0:
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    return _done(img)


def m_ring(cx: float, cy: float, r_out: float, r_in: float) -> np.ndarray:
    return m_disc(cx, cy, r_out) & ~m_disc(cx, cy, r_in)


def m_pie(cx: float, cy: float, r: float, start: float, end: float) -> np.ndarray:
    img, d = _surface()
    if r > 0:
        d.pieslice((cx - r, cy - r, cx + r, cy + r), start, end, fill=255)
    return _done(img)


def m_poly(points: list[tuple[float, float]]) -> np.ndarray:
    img, d = _surface()
    d.polygon(points, fill=255)
    return _done(img)


def m_rect(x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    """Half-open rectangle."""
    img, d = _surface()
    if x1 > x0 and y1 > y0:
        d.rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
    return _done(img)


def m_line(points: list[tuple[float, float]], width: int, caps: bool = True) -> np.ndarray:
    img, d = _surface()
    d.line(points, fill=255, width=width)
    if caps and width > 2:
        r = width / 2
        for x, y in (points[0], points[-1]):
            d.ellipse((x - r, y - r, x + r, y + r), fill=255)
    return _done(img)


def texture(kind: str, ink: int, ground: int, **kw: Any) -> np.ndarray:
    """A full-size flat texture as palette indices, made with ``Canvas`` tools.

    ``kind`` is ``stripes`` (``period``, ``duty``, ``angle``), ``dots``
    (``spacing``, ``radius``) or ``checker`` (``size``)."""
    c = Canvas(background=ground)
    full = (0, 0, W, H)
    if kind == "stripes":
        c.stripes(
            full,
            ink,
            ground,
            period=int(kw.get("period", 8)),
            duty=float(kw.get("duty", 0.5)),
            angle=int(kw.get("angle", 45)),
        )
    elif kind == "dots":
        c.dots(full, ink, None, spacing=int(kw.get("spacing", 10)), radius=int(kw.get("radius", 3)))
    elif kind == "checker":
        c.checker(full, ink, ground, size=int(kw.get("size", 4)))
    else:
        raise ValueError(f"unknown texture {kind!r}")
    return c.to_frame()


# --- noise ----------------------------------------------------------------------------


def _fade(t: np.ndarray) -> np.ndarray:
    return t * t * t * (t * (t * 6 - 15) + 10)


class Perlin:
    """2-D gradient noise with ``octaves`` of fractal detail.

    ``period`` is the feature size in pixels of the first octave.  Evaluate at
    any float coordinates (arrays broadcast); output is roughly in -0.7..0.7.
    """

    def __init__(
        self,
        rng: np.random.Generator,
        period: float,
        octaves: int = 1,
        *,
        gain: float = 0.5,
        lacunarity: float = 2.0,
    ) -> None:
        self.layers: list[tuple[float, np.ndarray, np.ndarray, float]] = []
        for o in range(max(1, octaves)):
            p = float(period) / (lacunarity**o)
            gw = int(math.ceil(W / p)) + 3
            gh = int(math.ceil(H / p)) + 3
            ang = rng.uniform(0.0, 2 * math.pi, (gh, gw))
            self.layers.append((p, np.cos(ang), np.sin(ang), gain**o))

    def __call__(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        total = np.zeros(np.broadcast(x, y).shape)
        norm = 0.0
        for p, gx, gy, amp in self.layers:
            u = x / p + 1.0
            v = y / p + 1.0
            i0 = np.floor(u).astype(np.int64)
            j0 = np.floor(v).astype(np.int64)
            fu = u - i0
            fv = v - j0
            gh, gw = gx.shape
            i0 %= gw
            j0 %= gh
            i1 = (i0 + 1) % gw
            j1 = (j0 + 1) % gh
            n00 = gx[j0, i0] * fu + gy[j0, i0] * fv
            n10 = gx[j0, i1] * (fu - 1) + gy[j0, i1] * fv
            n01 = gx[j1, i0] * fu + gy[j1, i0] * (fv - 1)
            n11 = gx[j1, i1] * (fu - 1) + gy[j1, i1] * (fv - 1)
            su = _fade(fu)
            sv = _fade(fv)
            a = n00 + su * (n10 - n00)
            b = n01 + su * (n11 - n01)
            total += amp * (a + sv * (b - a))
            norm += amp
        return total / norm


def normalise(a: np.ndarray, lo_pct: float = 2.0, hi_pct: float = 98.0) -> np.ndarray:
    """Stretch ``a`` to 0..1 between two percentiles (clipped)."""
    lo, hi = np.percentile(a, [lo_pct, hi_pct])
    if hi - lo < 1e-9:
        return np.zeros_like(a, dtype=np.float64)
    return np.clip((a - lo) / (hi - lo), 0.0, 1.0)


def smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def pixel_grid(box: Box) -> tuple[np.ndarray, np.ndarray]:
    """Pixel-centre coordinates ``(xs, ys)`` covering ``box`` (clipped to the card)."""
    x0, y0, x1, y1 = (max(0, box[0]), max(0, box[1]), min(W, box[2]), min(H, box[3]))
    ys, xs = np.mgrid[y0:y1, x0:x1]
    return xs.astype(np.float64) + 0.5, ys.astype(np.float64) + 0.5


def paint(idx: np.ndarray, box: Box, values: np.ndarray, where: np.ndarray | None = None) -> None:
    """Write a box-sized array of indices (optionally only ``where``) into ``idx``."""
    x0, y0, x1, y1 = (max(0, box[0]), max(0, box[1]), min(W, box[2]), min(H, box[3]))
    region = idx[y0:y1, x0:x1]
    if where is None:
        region[...] = values
    else:
        region[where] = values[where] if np.ndim(values) else values
