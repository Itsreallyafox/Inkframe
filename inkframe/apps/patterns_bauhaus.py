"""Bauhaus: 5-12 large primitives composed on a 6 x 4 grid of square cells.

The composition follows a small grammar rather than scattering shapes:

1. one *dominant* form in the hero colour (a big disc, a half disc rising from
   an edge, a thick ring, or a quarter disc swinging out of a corner), placed
   off-centre on a grid line so the card is asymmetric;
2. an *axis*: a black bar on a grid line, often crossing the dominant form,
   sometimes knocking it out where they overlap;
3. *counterweights*: drawn from a vocabulary of three or four primitives
   chosen per card (squares, discs, triangles, half discs, rings, quarter
   discs, bars) plus dots, one or two cells big, placed preferably on the
   side opposite the dominant form so the card balances;
4. one or two forms filled with ``stripes``/``dots`` texture instead of a flat
   colour, and occasional *overprints* (yellow over blue prints green, red over
   blue prints black) when the mood has the resulting colour.

Colour roles: the field is white or yellow, black is structure, one accent
is the hero and at most one more accent supports it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from inkframe.apps.draw import Canvas
from inkframe.apps.patterns_common import (
    FILL,
    LINE,
    Plate,
    Scheme,
    blit,
    box_mask,
    centre_grid,
    contrast,
    frame_of,
    m_disc,
    m_line,
    m_pie,
    m_poly,
    m_rect,
    m_ring,
    mount,
    pick_scheme,
    texture,
    weighted,
)
from inkframe.display.palette import BLACK, BLUE, GREEN, RED, WHITE, YELLOW

NAME = "Bauhaus"
_COUNT = {1: (5, 6), 2: (6, 8), 3: (7, 9), 4: (9, 11), 5: (10, 12)}
_HERO_W = {RED: 0.42, BLUE: 0.33, GREEN: 0.15, YELLOW: 0.10}
_KINDS = {
    "square": 1.0,
    "disc": 1.2,
    "tri": 1.3,
    "half": 1.0,
    "bar": 0.6,
    "ring": 0.6,
    "quarter": 0.9,
}

#: (existing colour, new colour) -> printed colour, used only when the mood allows it
_OVERPRINT = {
    (YELLOW, BLUE): GREEN,
    (BLUE, YELLOW): GREEN,
    (YELLOW, GREEN): GREEN,
    (GREEN, YELLOW): GREEN,
    (RED, BLUE): BLACK,
    (BLUE, RED): BLACK,
    (RED, GREEN): BLACK,
    (GREEN, RED): BLACK,
    (YELLOW, RED): RED,
    (RED, YELLOW): RED,
}


class _Grid:
    def __init__(self, box: tuple[int, int, int, int], cell: int) -> None:
        self.x0, self.y0 = box[0], box[1]
        self.c = cell

    def p(self, gx: float, gy: float) -> tuple[float, float]:
        return (self.x0 + gx * self.c, self.y0 + gy * self.c)


def _roles(rng: np.random.Generator, scheme: Scheme) -> tuple[int, int, int | None]:
    """``(structure, hero, second accent or None)``."""
    ground, allowed = scheme.ground, scheme.allowed
    struct = BLACK if BLACK in allowed else scheme.inks[0]
    heroes = {
        c: w
        for c, w in _HERO_W.items()
        if c in allowed and c != ground and contrast(c, ground) >= FILL
    }
    strong = {c: w for c, w in heroes.items() if contrast(c, ground) >= LINE}
    pool = strong or heroes
    hero = weighted(rng, pool) if pool else struct
    seconds = {c: w for c, w in heroes.items() if c != hero and contrast(c, hero) >= FILL}
    second = weighted(rng, seconds) if seconds and rng.random() < 0.65 else None
    return struct, hero, second


def _dominant(
    rng: np.random.Generator, g: _Grid, side: int, density: int
) -> tuple[np.ndarray, str, tuple[float, float]]:
    """Mask, kind and centre (grid units) of the dominant form."""
    c = g.c
    big = density <= 2
    kind = weighted(rng, {"disc": 0.34, "half": 0.24, "ring": 0.16, "quarter": 0.26})
    if kind == "disc":
        r = c * float(rng.choice([2.0, 2.0] if big else [1.5, 2.0, 2.0]))
        gx = 3 + side * float(rng.choice([1.0, 1.5]))
        gy = float(rng.choice([1.5, 2.0, 2.0, 2.5]))
        return m_disc(*g.p(gx, gy), r), kind, (gx, gy)
    if kind == "ring":
        r = c * float(rng.choice([1.5, 2.0]))
        t = c * float(rng.choice([0.4, 0.5, 0.6]))
        gx = 3 + side * float(rng.choice([1.0, 1.5]))
        gy = 2.0
        return m_ring(*g.p(gx, gy), r, r - t), kind, (gx, gy)
    if kind == "half":
        r = c * float(rng.choice([2.5, 3.0] if big else [2.0, 2.5, 3.0]))
        edge = weighted(rng, {"bottom": 0.45, "top": 0.2, "left": 0.175, "right": 0.175})
        if edge in ("left", "right"):
            gx = 0.0 if edge == "left" else 6.0
            gy = float(rng.choice([1.5, 2.0, 2.5]))
            start = -90.0 if edge == "left" else 90.0
        else:
            gx = 3 + side * float(rng.choice([0.5, 1.0, 1.5]))
            gy = 4.0 if edge == "bottom" else 0.0
            start = 180.0 if edge == "bottom" else 0.0
        return m_pie(*g.p(gx, gy), r, start, start + 180), kind, (gx, gy)
    # quarter disc out of a field corner on the dominant side
    r = c * float(rng.choice([3.0, 3.5, 4.0]))
    gx = 0.0 if side < 0 else 6.0
    gy = float(rng.choice([0.0, 4.0]))
    start = {(0.0, 0.0): 0, (6.0, 0.0): 90, (6.0, 4.0): 180, (0.0, 4.0): 270}[(gx, gy)]
    return m_pie(*g.p(gx, gy), r, start, start + 90), kind, (gx, gy)


def _axis(rng: np.random.Generator, g: _Grid) -> np.ndarray:
    c = g.c
    t = c * float(rng.choice([0.25, 1 / 3, 0.5]))
    length = int(rng.integers(3, 7))
    if rng.random() < 0.6:  # horizontal
        gy = float(rng.choice([1.0, 2.0, 3.0, 2.5, 1.5]))
        a = int(rng.integers(0, 7 - length))
        x0, y = g.p(a, gy)
        return m_rect(x0, y - t / 2, x0 + length * c, y + t / 2)
    length = min(length, 4)
    gx = float(rng.choice([1.0, 2.0, 4.0, 5.0, 3.0]))
    a = int(rng.integers(0, 5 - length))
    x, y0 = g.p(gx, a)
    return m_rect(x - t / 2, y0, x + t / 2, y0 + length * c)


def _secondary(
    rng: np.random.Generator, g: _Grid, ci: int, cj: int, k: int, kind: str
) -> np.ndarray:
    c = g.c
    bx0, by0 = g.p(ci, cj)
    bx1, by1 = g.p(ci + k, cj + k)
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    if kind == "square":
        inset = c * float(rng.choice([0.0, 0.0, 0.15]))
        return m_rect(bx0 + inset, by0 + inset, bx1 - inset, by1 - inset)
    if kind == "disc":
        return m_disc(cx, cy, k * c / 2)
    if kind == "dot":
        return m_disc(cx, cy, c * float(rng.uniform(0.16, 0.26)))
    if kind == "ring":
        r = k * c / 2
        return m_ring(cx, cy, r, r - c * float(rng.choice([0.2, 0.3])))
    if kind == "half":
        start = float(rng.choice([0, 90, 180, 270]))
        return m_pie(cx, cy, k * c / 2, start, start + 180)
    if kind == "quarter":
        corner = int(rng.integers(4))
        px, py = [(bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)][corner]
        start = [0, 90, 180, 270][corner]
        return m_pie(px, py, k * c, start, start + 90)
    if kind == "bar":
        t = c * float(rng.choice([0.25, 0.35]))
        if rng.random() < 0.5:
            return m_rect(bx0, cy - t / 2, bx1 + c * float(rng.choice([0, 1])), cy + t / 2)
        return m_rect(cx - t / 2, by0, cx + t / 2, by1)
    # triangle: right angle in a corner, or isosceles pointing a direction
    if rng.random() < 0.55:
        pts = [(bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)]
        drop = int(rng.integers(4))
        return m_poly([p for i, p in enumerate(pts) if i != drop])
    d = int(rng.integers(4))
    if d == 0:
        pts = [(cx, by0), (bx1, by1), (bx0, by1)]
    elif d == 1:
        pts = [(bx1, cy), (bx0, by1), (bx0, by0)]
    elif d == 2:
        pts = [(cx, by1), (bx0, by0), (bx1, by0)]
    else:
        pts = [(bx0, cy), (bx1, by0), (bx1, by1)]
    return m_poly(pts)


def _apply(
    idx: np.ndarray,
    m: np.ndarray,
    colour: int,
    *,
    ground: int,
    allowed: frozenset[int],
    blend: str,
    tex: np.ndarray | None,
) -> None:
    if not m.any():
        return
    if blend == "knock":
        inked = idx != ground
        idx[m & inked] = ground
        m = m & ~inked
    if tex is not None:
        idx[m] = tex[m]
        return
    if blend == "overprint":
        # mixes only where it crosses another ink; on the bare field it prints as itself
        under = idx[m]
        new = np.full(under.shape, colour, dtype=np.uint8)
        for (a, b), res in _OVERPRINT.items():
            if b == colour and a != ground and res in allowed:
                new[under == a] = res
        idx[m] = new
        return
    idx[m] = colour


def draw(rng: np.random.Generator, canvas: Canvas, settings: Mapping[str, Any]) -> Plate:
    density = int(settings.get("density", 3))
    mood = str(settings.get("palette_mood", "full"))
    plate = mount(rng, canvas, settings, bleed_p=0.4, frame_p=0.35)
    x0, y0, x1, y1 = plate.box
    cell = min((x1 - x0) // 6, (y1 - y0) // 4)
    plate.box = centre_grid(plate.box, cell, 6, 4)
    plate.title = NAME
    g = _Grid(plate.box, cell)
    scheme = pick_scheme(rng, mood, grounds={WHITE: 0.6, YELLOW: 0.4}, n=4, need=FILL)
    ground, allowed = scheme.ground, scheme.allowed
    struct, hero, second = _roles(rng, scheme)
    clip = box_mask(plate.box)
    idx = frame_of(canvas)
    idx[clip] = ground

    side = -1 if rng.random() < 0.5 else 1
    lo, hi = _COUNT.get(density, (7, 9))
    total = int(rng.integers(lo, hi + 1))
    shapes: list[dict[str, Any]] = []

    m, kind, (hgx, hgy) = _dominant(rng, g, side, density)
    shapes.append({"m": m, "c": hero, "order": 0, "blend": "over"})
    plate.note = {"disc": "disc", "half": "rising half", "ring": "ring", "quarter": "quarter"}[kind]
    shapes.append(
        {
            "m": _axis(rng, g),
            "c": struct,
            "order": 2,
            "blend": "knock" if rng.random() < 0.3 else "over",
        }
    )

    # this card's vocabulary: three or four primitives, plus dots
    names = list(_KINDS)
    p = np.array([_KINDS[n] for n in names])
    vocab = list(rng.choice(names, size=int(rng.integers(3, 5)), replace=False, p=p / p.sum()))
    kinds = {str(n): _KINDS[str(n)] for n in vocab}
    kinds["dot"] = 0.5
    palette_w = {struct: 0.45, hero: 0.3}
    if second is not None:
        palette_w[second] = 0.25
    else:
        palette_w = {struct: 0.55, hero: 0.45}
    occupancy = np.zeros((4, 6))
    textured, max_tex = 0, 1 if density <= 3 else 2
    for _ in range(max(0, total - 2)):
        kind_s = weighted(rng, kinds)
        k = 2 if (kind_s not in ("dot", "bar") and rng.random() < 0.35) else 1
        cand, weights = [], []
        for cj in range(0, 5 - k):
            for ci in range(0, 7 - k):
                same_side = (ci + k / 2 - 3) * side > 0
                crowd = occupancy[cj : cj + k, ci : ci + k].sum()
                cand.append((ci, cj))
                weights.append((0.35 if same_side else 1.0) / (1 + 1.6 * crowd))
        pw = np.array(weights) / np.sum(weights)
        ci, cj = cand[int(rng.choice(len(cand), p=pw))]
        occupancy[cj : cj + k, ci : ci + k] += 1
        ms = _secondary(rng, g, ci, cj, k, kind_s)
        colour = weighted(rng, palette_w)
        tex = None
        if textured < max_tex and k == 2 and kind_s in ("square", "disc", "half", "quarter", "tri"):
            if rng.random() < 0.55:
                textured += 1
                ink = struct if colour == ground else colour
                if rng.random() < 0.7:
                    tex = texture(
                        "stripes",
                        ink,
                        ground,
                        period=int(rng.choice([8, 10, 12])),
                        duty=0.5,
                        angle=int(rng.choice([0, 45, 90, 45])),
                    )
                else:
                    tex = texture("dots", struct, ground, spacing=12, radius=3)
        blend = weighted(rng, {"over": 0.74, "overprint": 0.16, "knock": 0.10})
        order = 4 if kind_s == "dot" else (1 if k == 2 else 3)
        shapes.append({"m": ms, "c": colour, "order": order, "blend": blend, "tex": tex})

    if rng.random() < 0.18:  # the line against the point: from a far corner to the hero
        far = g.p(6.0 if side < 0 else 0.0, float(rng.choice([0.0, 4.0])))
        shapes.append(
            {
                "m": m_line([far, g.p(hgx, hgy)], 6, caps=False),
                "c": struct,
                "order": 3,
                "blend": "over",
            }
        )

    shapes.sort(key=lambda s: s["order"])
    swaps = [c for c in palette_w if c != ground]
    for s in shapes:
        m = s["m"] & clip
        colour = s["c"]
        if s.get("tex") is None and s["blend"] != "knock" and m.any():
            # a form landing mostly on its own colour would merge into a blob:
            # print it in another colour of the card instead
            under = idx[m]
            if np.count_nonzero(under == colour) > 0.25 * under.size:
                alts = [
                    c
                    for c in swaps
                    if c != colour and np.count_nonzero(under == c) < 0.25 * under.size
                ]
                if alts:
                    colour = alts[0]
        _apply(
            idx,
            m,
            colour,
            ground=ground,
            allowed=allowed,
            blend=s["blend"],
            tex=s.get("tex"),
        )
    blit(canvas, idx)
    return plate
