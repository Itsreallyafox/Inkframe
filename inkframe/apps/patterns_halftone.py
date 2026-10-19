"""Halftone: a printed screen whose dot size follows one large smooth shape.

The screen is a regular grid (rotated 0, 15, 30 or 45 degrees, like real
process screens) of round dots, square dots or a horizontal line screen
(diagonal lines staircase without anti-aliasing, vertical ones read as a
barcode).  Each
cell's dot size comes from a tone function evaluated at the cell centre, so
from a distance the card reads as a halftone print of a big form:

* ``sphere``  a lit ball, on a floor with a cast shadow, on a dark ground, or alone
* ``glow``    one or two radial falloffs, like suns
* ``waves``   undulating bands
* ``ripple``  concentric waves fading out
* ``noise``   a seeded fractal cloud
* ``eclipse`` a linear gradient reversed inside a big disc (op art)

Print modes: one screen on the ground; one screen over a *flat underlay*
(the motif's shape in a solid accent, the way a poster prints a halftone
over a colour block); or one screen in two inks, the motif's dots in one and
the surrounding dots in the other.  (Two overlapping screens at different
angles were tried and read as speckle on this panel.)

Vectorised: each pixel finds its nearest screen cell and compares its
distance with that cell's dot radius, so dots are exact and merge into solid
areas at full tone.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from inkframe.apps.draw import Canvas
from inkframe.apps.patterns_common import (
    FILL,
    LINE,
    Perlin,
    Plate,
    blit,
    contrast,
    frame_of,
    mount,
    paint,
    pick_scheme,
    pixel_grid,
    smoothstep,
    weighted,
)
from inkframe.display.palette import BLACK, BLUE, GREEN, NAMES, RED, WHITE, YELLOW

NAME = "Halftone"
_SPACING = {1: 16, 2: 14, 3: 12, 4: 10, 5: 9}
_MOTIFS = {
    "sphere": 0.24,
    "glow": 0.16,
    "waves": 0.15,
    "ripple": 0.15,
    "noise": 0.15,
    "eclipse": 0.15,
}
_UNDERLAYS = (YELLOW, RED, BLUE, GREEN, BLACK, WHITE)
#: rank of each colour by lightness on the panel (measured L*)
_LIGHTNESS = {BLACK: 0, BLUE: 1, RED: 2, GREEN: 3, YELLOW: 4, WHITE: 5}

Tone = Callable[[np.ndarray, np.ndarray], np.ndarray]
Shape = Callable[[np.ndarray, np.ndarray], np.ndarray]


def _tone(
    rng: np.random.Generator, motif: str, box: tuple[int, int, int, int], light_ink: bool
) -> tuple[Tone, Shape]:
    """The tone function (0 = bare ground, 1 = solid ink) and the motif's flat
    shape.  ``light_ink``: the ink is lighter than the ground, so a lit
    surface needs *more* ink, not less."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    if motif == "sphere":
        r = bh * float(rng.uniform(0.34, 0.44))
        cx = x0 + bw * float(rng.uniform(0.3, 0.7))
        cy = y0 + bh * float(rng.uniform(0.4, 0.52))
        lx = float(rng.uniform(0.35, 0.9)) * float(rng.choice([-1, 1]))
        lv = np.array([lx, -float(rng.uniform(0.45, 0.9)), 0.75])
        lv = lv / np.linalg.norm(lv)
        bg = weighted(rng, {"floor": 0.45, "dark": 0.3, "plain": 0.25})
        shade = float(rng.uniform(1.0, 1.5))

        def sphere(x: np.ndarray, y: np.ndarray) -> np.ndarray:
            dx, dy = (x - cx) / r, (y - cy) / r
            rr = dx * dx + dy * dy
            z = np.sqrt(np.clip(1 - rr, 0, 1))
            lam = np.clip(dx * lv[0] + dy * lv[1] + z * lv[2], 0, 1)
            # darkness of every surface, 0 = fully lit
            inside = 0.03 + 0.97 * (1 - lam) ** shade
            if bg == "dark":
                outside = np.full(np.shape(rr), 0.55)
            elif bg == "floor":
                hor = cy + r * 0.55
                floor = 0.12 + 0.25 * smoothstep(hor, y1, y)
                outside = np.where(y > hor, floor, 0.0 if not light_ink else 1.0)
                sx = cx - lv[0] * r * 0.7
                sh = ((x - sx) / (r * 1.15)) ** 2 + ((y - (cy + r * 0.97)) / (r * 0.24)) ** 2
                outside = np.where(sh < 1, np.maximum(outside, 0.95 - 0.5 * sh), outside)
            else:
                outside = np.zeros(np.shape(rr)) if not light_ink else np.ones(np.shape(rr))
            dark = np.where(rr < 1, inside, outside)
            return 1 - dark if light_ink else dark

        return sphere, lambda x, y: np.hypot(x - cx, y - cy) < r
    if motif == "glow":
        n = int(rng.choice([1, 1, 2]))
        cs = [
            (
                x0 + bw * float(rng.uniform(0.2, 0.8)),
                y0 + bh * float(rng.uniform(0.25, 0.75)),
                bh * float(rng.uniform(0.45, 0.8)),
            )
            for _ in range(n)
        ]
        gamma = float(rng.uniform(0.8, 1.5))

        def glow(x: np.ndarray, y: np.ndarray) -> np.ndarray:
            t = np.zeros(np.broadcast(x, y).shape)
            for cx, cy, r in cs:
                t = np.maximum(t, np.clip(1 - np.hypot(x - cx, y - cy) / r, 0, 1) ** gamma)
            return t

        gx, gy, gr = cs[0]
        return glow, lambda x, y: np.hypot(x - gx, y - gy) < gr * 0.8
    if motif == "waves":
        lam = bh * float(rng.uniform(0.45, 0.8))
        lam_x = bw * float(rng.uniform(0.5, 1.2))
        amp = float(rng.uniform(0.15, 0.4))
        ph = float(rng.uniform(0, 2 * math.pi))
        tilt = float(rng.uniform(-0.3, 0.3))

        def waves(x: np.ndarray, y: np.ndarray) -> np.ndarray:
            s = (y - y0 + tilt * (x - x0)) / lam + amp * np.sin(2 * math.pi * (x - x0) / lam_x + ph)
            return 0.5 + 0.5 * np.sin(2 * math.pi * s)

        return waves, lambda x, y: waves(x, y) > 0.62
    if motif == "ripple":
        cx = x0 + bw * float(rng.uniform(0.25, 0.75))
        cy = y0 + bh * float(rng.uniform(0.3, 0.7))
        lam = float(rng.uniform(56, 90))
        reach = bw * float(rng.uniform(0.55, 0.9))

        def ripple(x: np.ndarray, y: np.ndarray) -> np.ndarray:
            d = np.hypot(x - cx, y - cy)
            fade = np.clip(1 - d / reach, 0, 1) ** 0.7
            return (0.5 + 0.5 * np.cos(2 * math.pi * d / lam)) * fade

        return ripple, lambda x, y: np.hypot(x - cx, y - cy) < lam * 0.5
    if motif == "noise":
        noise = Perlin(rng, period=float(rng.uniform(210, 320)), octaves=2)
        # normalise against a coarse sample of the field so contrast is stable
        sy, sx = np.mgrid[y0:y1:8, x0:x1:8]
        lo, hi = np.percentile(noise(sx + 0.5, sy + 0.5), [3, 97])
        gamma = float(rng.uniform(1.1, 1.8))

        def cloud(x: np.ndarray, y: np.ndarray) -> np.ndarray:
            t = np.clip((noise(x, y) - lo) / max(1e-6, hi - lo), 0, 1)
            return smoothstep(0.05, 0.95, t) ** gamma

        return cloud, lambda x, y: cloud(x, y) > 0.5
    # eclipse
    ang = float(rng.uniform(0, 2 * math.pi))
    ux, uy = math.cos(ang), math.sin(ang)
    cx = x0 + bw * float(rng.uniform(0.3, 0.7))
    cy = y0 + bh * float(rng.uniform(0.35, 0.65))
    r = bh * float(rng.uniform(0.3, 0.42))
    half = 0.5 * (abs(ux) * bw + abs(uy) * bh)
    mx, my = x0 + bw / 2, y0 + bh / 2

    def eclipse(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        g = np.clip(0.5 + ((x - mx) * ux + (y - my) * uy) / (2 * half), 0, 1)
        return np.where(np.hypot(x - cx, y - cy) < r, 1 - g, g)

    return eclipse, lambda x, y: np.hypot(x - cx, y - cy) < r


def screen(
    xs: np.ndarray,
    ys: np.ndarray,
    spacing: float,
    angle: float,
    tone: Tone,
    dot: str,
    origin: tuple[float, float],
) -> np.ndarray:
    """Boolean ink mask of a halftone screen over pixel centres ``xs``/``ys``."""
    a = math.radians(angle)
    ca, sa = math.cos(a), math.sin(a)
    ox, oy = origin
    X, Y = xs - ox, ys - oy
    u = X * ca + Y * sa
    v = -X * sa + Y * ca
    iv = np.round(v / spacing)
    dv = v - iv * spacing
    if dot == "line":
        cu, cv = u, iv * spacing
    else:
        iu = np.round(u / spacing)
        du = u - iu * spacing
        cu, cv = iu * spacing, iv * spacing
    cx = ox + cu * ca - cv * sa
    cy = oy + cu * sa + cv * ca
    t = np.clip(tone(cx, cy), 0.0, 1.0)
    if dot == "line":
        half = 0.5 * spacing * t
        return (np.abs(dv) < half) & (half >= 0.9)
    if dot == "square":
        half = 0.5 * spacing * np.sqrt(t) * 1.06
        return (np.maximum(np.abs(du), np.abs(dv)) < half) & (half >= 1.2)
    r = spacing * 0.72 * np.sqrt(t)
    return (np.hypot(du, dv) < r) & (r >= 1.4)


def draw(rng: np.random.Generator, canvas: Canvas, settings: Mapping[str, Any]) -> Plate:
    density = int(settings.get("density", 3))
    mood = str(settings.get("palette_mood", "full"))
    plate = mount(rng, canvas, settings, bleed_p=0.45, frame_p=0.2)
    motif = weighted(rng, _MOTIFS)
    plate.title, plate.note = NAME, motif
    scheme = pick_scheme(
        rng,
        mood,
        grounds={WHITE: 0.6, YELLOW: 0.16, BLACK: 0.12, BLUE: 0.12},
        n=3,
        need=LINE,
        lead_black=0.5,
    )
    spacing = float(_SPACING.get(density, 12)) * float(rng.choice([1.0, 1.0, 1.15]))
    box = plate.box
    xs, ys = pixel_grid(box)
    light_ink = _LIGHTNESS[scheme.inks[0]] > _LIGHTNESS[scheme.ground]
    tone, shape = _tone(rng, motif, box, light_ink)
    dot = weighted(rng, {"round": 0.6, "line": 0.22, "square": 0.18})
    if dot == "line":
        angle = 0.0  # vertical line screens read as barcodes
    else:
        angle = float(weighted(rng, {45: 0.45, 15: 0.2, 30: 0.15, 0: 0.2}))
    origin = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    ground, ink = scheme.ground, scheme.inks[0]
    out = np.full(xs.shape, ground, dtype=np.uint8)
    mode = weighted(rng, {"plain": 0.45, "underlay": 0.37, "two": 0.18})
    if mode == "underlay" and (dot == "square" or motif == "noise"):
        # square dots leave a 1 px lattice that an underlay turns into a mesh,
        # and a cloud has no single shape to underlay
        mode = "two"
    if mode == "underlay":
        under = [
            c
            for c in _UNDERLAYS
            if c in scheme.allowed
            and c not in (ground, ink)
            and contrast(c, ground) >= FILL
            and contrast(ink, c) >= FILL
        ]
        # an underlay darker than a dark ink (or lighter than a light one)
        # would light the motif from the wrong side
        if light_ink:
            under = [c for c in under if _LIGHTNESS[c] < _LIGHTNESS[ink]]
        else:
            under = [c for c in under if _LIGHTNESS[c] > _LIGHTNESS[ink]]
        under = [c for c in under if contrast(ink, c) >= LINE] or under
        if under:
            accent = under[int(rng.integers(len(under)))]
            out[shape(xs, ys)] = accent
            plate.note = f"{motif} on {NAMES[accent]}"
    m1 = screen(xs, ys, spacing, angle, tone, dot, origin)
    if mode == "two" and len(scheme.inks) > 1:
        # one screen, two inks: the motif's dots in one colour, the rest in another
        # (overlapping screens at different angles turn to speckle on this panel)
        inside = shape(xs, ys)
        out[m1 & ~inside] = scheme.inks[1]
        out[m1 & inside] = ink
        plate.note = f"{motif}, two inks"
    else:
        out[m1] = ink
    idx = frame_of(canvas)
    paint(idx, box, out)
    blit(canvas, idx)
    return plate
