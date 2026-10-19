"""Concentric forms after Albers and Vasarely.

Styles:

* ``albers``: Homage-to-the-Square studies: three or four nested squares
  stacked low (Albers' 1 : 1.5 : 0.5 margins), as a single print, a
  triptych or a 3 x 2 sheet of colour studies that permute one palette;
* ``rings``: bands around one centre (sometimes a corner, which makes a
  rainbow) with a width rhythm (even, growing, pulsing, irregular) and a
  region where the colours invert;
* ``interference``: two ring systems combined by parity, a moire;
* ``arcs``: a grid of big tiles, each a set of quarter-circle bands out of
  one corner (seventies supergraphics);
* ``squares``: nested squares, diamonds or plate-proportioned rectangles,
  also with an inversion region.

Bands are computed per pixel with numpy (distance, then band index), so
edges are crisp and nothing is anti-aliased.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from inkframe.apps.draw import Canvas
from inkframe.apps.patterns_common import (
    FILL,
    LINE,
    Plate,
    best_cell,
    blit,
    centre_grid,
    contrast,
    fill_box,
    frame_of,
    mount,
    paint,
    pick_scheme,
    pixel_grid,
    weighted,
)
from inkframe.display.palette import BLACK, WHITE, YELLOW

NAME = "Concentric"
_STYLES = {"albers": 0.2, "rings": 0.24, "interference": 0.16, "arcs": 0.22, "squares": 0.18}
_BAND = {1: 30, 2: 24, 3: 19, 4: 15, 5: 12}
_TILE = {1: 200, 2: 180, 3: 150, 4: 120, 5: 100}


def _edges(rng: np.random.Generator, base: float, rhythm: str, reach: float) -> np.ndarray:
    """Cumulative band edges out to ``reach`` for a width rhythm."""
    out: list[float] = []
    acc, k = 0.0, 0
    while acc < reach and k < 400:
        if rhythm == "grow":
            w = base * 0.45 * (1.13**k)
        elif rhythm == "pulse":
            w = base * (1.7 if k % 2 == 0 else 0.55)
        elif rhythm == "random":
            w = base * float(rng.choice([0.5, 1.0, 1.0, 2.0]))
        else:
            w = base
        acc += max(6.0, w)
        out.append(acc)
        k += 1
    return np.array(out)


def _cycle(rng: np.random.Generator, scheme: Any, ground: int) -> list[int]:
    """Band colours in order; every neighbour pair (including the wrap) contrasts."""
    inks = list(scheme.inks)
    options = [[inks[0], ground]]
    if len(inks) >= 2:
        options.append([inks[0], ground, inks[1], ground])
        if all(
            contrast(a, b) >= FILL for a, b in zip(inks[:3], inks[1:3] + inks[:1], strict=False)
        ):
            options.append(inks[:3] if len(inks) >= 3 else [inks[0], inks[1], ground])
    return options[int(rng.integers(len(options)))]


def _invert_region(
    rng: np.random.Generator, xs: np.ndarray, ys: np.ndarray, box: tuple[int, int, int, int]
) -> np.ndarray | None:
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    kind = weighted(rng, {"none": 0.3, "half": 0.3, "disc": 0.25, "quadrants": 0.15})
    if kind == "half":
        a = float(rng.uniform(0, math.pi))
        px = x0 + bw * float(rng.uniform(0.35, 0.65))
        py = y0 + bh * float(rng.uniform(0.35, 0.65))
        return ((xs - px) * math.cos(a) + (ys - py) * math.sin(a)) > 0
    if kind == "disc":
        cx = x0 + bw * float(rng.uniform(0.25, 0.75))
        cy = y0 + bh * float(rng.uniform(0.3, 0.7))
        return np.hypot(xs - cx, ys - cy) < bh * float(rng.uniform(0.22, 0.38))
    if kind == "quadrants":
        cx = x0 + bw * float(rng.uniform(0.35, 0.65))
        cy = y0 + bh * float(rng.uniform(0.35, 0.65))
        return (xs > cx) ^ (ys > cy)
    return None


def _albers(rng: np.random.Generator, canvas: Canvas, plate: Plate, scheme: Any) -> str:
    x0, y0, x1, y1 = plate.box
    bw, bh = x1 - x0, y1 - y0
    layout = weighted(rng, {"single": 0.3, "triptych": 0.35, "sheet": 0.35})
    levels = 4 if rng.random() < 0.6 else 3
    colours = sorted(scheme.allowed)
    # candidate sequences: outer to inner, neighbours readable, outer not the paper
    seqs: list[list[int]] = []
    for _ in range(400):
        seq = [colours[int(i)] for i in rng.permutation(len(colours))[:levels]]
        if len(seq) < levels:
            seq = [seq[k % len(seq)] for k in range(levels)]
        if seq[0] == WHITE:
            continue
        if all(contrast(a, b) >= FILL for a, b in zip(seq, seq[1:], strict=False)):
            seqs.append(seq)
        if len(seqs) >= 6:
            break
    if not seqs:
        alt = [scheme.inks[0], WHITE]
        seqs = [[alt[k % 2] for k in range(levels)]]
    if layout == "single":
        side = min(bw, bh)
        boxes = [((x0 + x1 - side) // 2, y0 + (bh - side) // 2, side)]
    elif layout == "triptych":
        gap = 18
        side = min((bw - 2 * gap) // 3, bh)
        sx = x0 + (bw - (3 * side + 2 * gap)) // 2
        sy = y0 + (bh - side) // 2
        boxes = [(sx + k * (side + gap), sy, side) for k in range(3)]
    else:
        gap = 14
        side = min((bw - 2 * gap) // 3, (bh - gap) // 2)
        sx = x0 + (bw - (3 * side + 2 * gap)) // 2
        sy = y0 + (bh - (2 * side + gap)) // 2
        boxes = [
            (sx + i * (side + gap), sy + j * (side + gap), side) for j in range(2) for i in range(3)
        ]
    for n, (bx, by, side) in enumerate(boxes):
        seq = seqs[n % len(seqs)] if layout != "single" else seqs[0]
        u = side / (2 * levels + 2)
        for k in range(levels):
            s = side - 2 * k * u
            left = bx + k * u
            top = by + 1.5 * k * u
            fill_box(canvas, (round(left), round(top), round(left + s), round(top + s)), seq[k])
    xs0 = min(b[0] for b in boxes)
    ys0 = min(b[1] for b in boxes)
    xs1 = max(b[0] + b[2] for b in boxes)
    ys1 = max(b[1] + b[2] for b in boxes)
    plate.box = (xs0, ys0, xs1, ys1)
    return {"single": "homage", "triptych": "homage triptych", "sheet": "colour studies"}[layout]


def draw(rng: np.random.Generator, canvas: Canvas, settings: Mapping[str, Any]) -> Plate:
    density = int(settings.get("density", 3))
    mood = str(settings.get("palette_mood", "full"))
    style = weighted(rng, _STYLES)
    plate = mount(rng, canvas, settings, bleed_p=0.0 if style == "albers" else 0.4, frame_p=0.25)
    plate.title = NAME
    if style == "albers":
        scheme = pick_scheme(rng, mood, grounds={WHITE: 1.0}, n=4, need=FILL)
        plate.note = _albers(rng, canvas, plate, scheme)
        plate.frame = False
        return plate

    scheme = pick_scheme(
        rng,
        mood,
        grounds={WHITE: 0.55, YELLOW: 0.22, BLACK: 0.23},
        n=3,
        need=LINE,  # bands are 10-30 px: they read like lines, not fills
        lead_black=0.4,
    )
    ground = scheme.ground
    base = float(_BAND.get(density, 19)) * float(rng.uniform(0.85, 1.25))
    box = plate.box
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    idx = frame_of(canvas)

    if style == "arcs":
        target = _TILE.get(density, 150)
        if plate.bleed:
            s = target
            nx, ny = -(-600 // s), -(-400 // s)
            gx0, gy0 = (600 - nx * s) // 2, (400 - ny * s) // 2
        else:
            s, nx, ny = best_cell(box, target, spread=14)
            plate.box = centre_grid(box, s, nx, ny)
            box = plate.box
            gx0, gy0 = box[0], box[1]
        xs, ys = pixel_grid(box)
        u, v = xs - gx0, ys - gy0
        ii = np.clip((u // s).astype(np.int64), 0, nx - 1)
        jj = np.clip((v // s).astype(np.int64), 0, ny - 1)
        fx, fy = u - ii * s, v - jj * s
        mode = weighted(rng, {"random": 0.4, "mirror": 0.35, "same": 0.25})
        if mode == "same":
            corners = np.full((ny, nx), int(rng.integers(4)))
        elif mode == "mirror":
            jg, ig = np.mgrid[0:ny, 0:nx]
            base_c = int(rng.integers(4))
            hx = (ig >= (nx + 1) // 2).astype(int)
            vy = (jg >= (ny + 1) // 2).astype(int)
            table = np.array([[0, 1, 2, 3], [1, 0, 3, 2], [3, 2, 1, 0], [2, 3, 0, 1]])
            corners = table[hx + 2 * vy, base_c]
        else:
            corners = rng.integers(0, 4, (ny, nx))
        c = corners[jj, ii]
        cx = np.where((c == 1) | (c == 2), s, 0)
        cy = np.where(c >= 2, s, 0)
        d = np.hypot(fx - cx, fy - cy)
        n_bands = int(rng.integers(3, 6))
        cycle = _cycle(rng, scheme, ground)
        if cycle[-1] != ground and rng.random() < 0.5:
            cycle = [*cycle, ground]
        band = np.floor(d / (s / n_bands)).astype(np.int64)
        offset = (ii + jj) % 2 if rng.random() < 0.4 else 0
        colours = np.array(cycle, dtype=np.uint8)[(band + offset) % len(cycle)]
        out = np.where(d < s, colours, ground).astype(np.uint8)
        paint(idx, box, out)
        plate.note = "quarter arcs"
        blit(canvas, idx)
        return plate

    xs, ys = pixel_grid(box)
    cycle = _cycle(rng, scheme, ground)
    if style == "interference":
        w = max(10.0, base * 0.8)
        sep = bw * float(rng.uniform(0.18, 0.4))
        ang = float(rng.uniform(-0.5, 0.5))
        mx, my = x0 + bw / 2, y0 + bh / 2
        c1 = (mx - sep / 2 * math.cos(ang), my - sep / 2 * math.sin(ang))
        c2 = (mx + sep / 2 * math.cos(ang), my + sep / 2 * math.sin(ang))
        b1 = np.floor(np.hypot(xs - c1[0], ys - c1[1]) / w).astype(np.int64) % 2
        b2 = np.floor(np.hypot(xs - c2[0], ys - c2[1]) / w).astype(np.int64) % 2
        ink = scheme.inks[0]
        out = np.where((b1 ^ b2) == 1, ink, ground).astype(np.uint8)
        if len(scheme.inks) > 1 and rng.random() < 0.5:
            out[(b1 & b2) == 1] = scheme.inks[1]
        paint(idx, box, out)
        plate.note = "interference"
        blit(canvas, idx)
        return plate

    if style == "rings":
        if rng.random() < 0.22:
            cx = float(rng.choice([x0, x1]))
            cy = float(rng.choice([y0, y1]))
            plate.note = "rainbow"
        else:
            cx = x0 + bw * float(rng.uniform(0.3, 0.7))
            cy = y0 + bh * float(rng.uniform(0.35, 0.65))
            plate.note = "rings"
        d = np.hypot(xs - cx, ys - cy)
    else:
        cx = x0 + bw * float(rng.uniform(0.38, 0.62))
        cy = y0 + bh * float(rng.uniform(0.4, 0.6))
        metric = weighted(rng, {"square": 0.4, "diamond": 0.3, "plate": 0.3})
        dx, dy = np.abs(xs - cx), np.abs(ys - cy)
        if metric == "square":
            d = np.maximum(dx, dy)
        elif metric == "diamond":
            d = (dx + dy) / math.sqrt(2)
        else:
            d = np.maximum(dx * bh / bw, dy)
        plate.note = {"square": "squares", "diamond": "diamonds", "plate": "rectangles"}[metric]
    rhythm = weighted(rng, {"even": 0.3, "grow": 0.25, "pulse": 0.25, "random": 0.2})
    edges = _edges(rng, base, rhythm, float(d.max()) + 1)
    band = np.searchsorted(edges, d, side="right")
    inv = _invert_region(rng, xs, ys, box)
    if inv is not None:
        band = band + inv.astype(np.int64)
        plate.note += " inverted"
    out = np.array(cycle, dtype=np.uint8)[band % len(cycle)]
    paint(idx, box, out)
    blit(canvas, idx)
    return plate
