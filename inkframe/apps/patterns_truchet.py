"""Truchet tiles: a grid of 40-60 px tiles, each placed at a random rotation.

Five tile sets, all rasterised per pixel (numpy) so strokes run seamlessly
from tile to tile:

* ``arcs``: Smith tiles, two quarter arcs joining edge midpoints; the
  continuous paths they form are found with a union-find and each path gets
  one colour, so the eye can follow a red line through a black maze;
* ``filled``: the same arcs as a two-colour fill (vertex parity colouring:
  every closed region is one solid colour), sometimes with pips;
* ``double``: arcs at 1/3 and 2/3 of the tile, two parallel lines per path;
* ``maze``: the "10 PRINT" diagonals with rounded joints;
* ``quilt``: half-square triangles, four rotations.

Rotations come from one of four generators: independent, mirror symmetric
(the quilt look), noise-biased (regions that agree) or a regular pattern
with deliberate glitches.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from inkframe.apps.draw import Canvas
from inkframe.apps.patterns_common import (
    FILL,
    LINE,
    H,
    Perlin,
    Plate,
    W,
    best_cell,
    blit,
    centre_grid,
    contrast,
    frame_of,
    m_line,
    mount,
    paint,
    pick_scheme,
    pixel_grid,
    weighted,
)
from inkframe.display.palette import BLACK, BLUE, WHITE, YELLOW

NAME = "Truchet"
_STYLES = {"arcs": 0.28, "filled": 0.22, "double": 0.16, "maze": 0.14, "quilt": 0.20}
_NOTES = {
    "arcs": "smith arcs",
    "filled": "filled arcs",
    "double": "double arcs",
    "maze": "ten print",
    "quilt": "half squares",
}
_TILE = {1: 60, 2: 55, 3: 50, 4: 45, 5: 40}

# how a reflection maps tile states: 2-state tiles swap, quilt corners swap pairwise
_HFLIP = {2: (1, 0), 4: (1, 0, 3, 2)}
_VFLIP = {2: (1, 0), 4: (3, 2, 1, 0)}


class _Union:
    def __init__(self) -> None:
        self.parent: dict[Any, Any] = {}

    def find(self, a: Any) -> Any:
        p = self.parent.setdefault(a, a)
        while p != self.parent[p]:
            self.parent[p] = self.parent[self.parent[p]]
            p = self.parent[p]
        self.parent[a] = p
        return p

    def join(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _states(
    rng: np.random.Generator, nx: int, ny: int, n: int, mode: str, *, tiles: bool = True
) -> np.ndarray:
    """``(ny, nx)`` values in ``0..n-1``.  ``tiles``: the values are tile
    rotations, so a mirrored copy also mirrors each tile; otherwise (colour
    indices) mirroring only copies values."""
    if mode == "mirror":
        hx, hy = (nx + 1) // 2, (ny + 1) // 2
        q = rng.integers(0, n, (hy, hx))
        out = np.zeros((ny, nx), dtype=np.int64)
        ident = tuple(range(n))
        hf = _HFLIP.get(n, ident) if tiles else ident
        vf = _VFLIP.get(n, ident) if tiles else ident
        for j in range(ny):
            for i in range(nx):
                qi = i if i < hx else nx - 1 - i
                qj = j if j < hy else ny - 1 - j
                st = int(q[qj, qi])
                if i >= hx:
                    st = hf[st]
                if j >= hy:
                    st = vf[st]
                out[j, i] = st
        return out
    if mode == "noise":
        noise = Perlin(rng, period=float(rng.uniform(2.5, 5.0)), octaves=2)
        jj, ii = np.mgrid[0:ny, 0:nx]
        v = noise(ii.astype(float), jj.astype(float)) + rng.normal(0, 0.12, (ny, nx))
        if n == 2:
            return (v > 0).astype(np.int64)
        return np.clip(((v + 0.5) * 4).astype(np.int64), 0, 3)
    if mode == "pattern":
        jj, ii = np.mgrid[0:ny, 0:nx]
        kind = int(rng.integers(3))
        if n == 2:
            base = [(ii + jj) % 2, (ii // 2 + jj) % 2, ii % 2][kind]
        else:
            base = [(ii % 2) + 2 * (jj % 2), (ii + jj) % 4, ((ii // 2) + (jj // 2)) % 4][kind]
            if kind == 0:  # pinwheels: 0 1 / 3 2
                base = np.array([0, 1, 3, 2])[base]
        flip = rng.random((ny, nx)) < rng.uniform(0.06, 0.16)
        noise_st = rng.integers(0, n, (ny, nx))
        return np.where(flip, noise_st, base).astype(np.int64)
    return rng.integers(0, n, (ny, nx)).astype(np.int64)


def _grid(plate: Plate, target: int) -> tuple[int, int, int, int, int]:
    """``(s, nx, ny, gx0, gy0)``; on a mounted plate the plate box is snapped."""
    if plate.bleed:
        s = target
        nx, ny = -(-W // s), -(-H // s)
        return s, nx, ny, (W - nx * s) // 2, (H - ny * s) // 2
    s, nx, ny = best_cell(plate.box, target)
    plate.box = centre_grid(plate.box, s, nx, ny)
    return s, nx, ny, plate.box[0], plate.box[1]


def _arc_paths(states: np.ndarray, double: bool) -> dict[tuple[int, int, int], Any]:
    """Union-find the edge points each arc joins; key ``(j, i, arc)`` -> path id.

    Arc numbering per tile: 0 = first corner (TL for state 0, TR for state 1),
    1 = second corner (BR / BL); with ``double`` 0..3 = corner A r1, A r2,
    B r1, B r2.  Edge points: ``("h", i, j, k)`` on the horizontal line above
    row ``j``, ``("v", i, j, k)`` on the vertical line left of column ``i``;
    ``k`` = 0 at 1/3 and 1 at 2/3 of the edge (or the midpoint when single)."""
    ny, nx = states.shape
    uf = _Union()
    arcs: dict[tuple[int, int, int], tuple[Any, Any]] = {}
    for j in range(ny):
        for i in range(nx):
            top, bottom = ("h", i, j), ("h", i, j + 1)
            left, right = ("v", i, j), ("v", i + 1, j)
            if not double:
                if states[j, i] == 0:
                    pairs = [((*top, 0), (*left, 0)), ((*right, 0), (*bottom, 0))]
                else:
                    pairs = [((*top, 0), (*right, 0)), ((*left, 0), (*bottom, 0))]
            elif states[j, i] == 0:
                pairs = [
                    ((*top, 0), (*left, 0)),
                    ((*top, 1), (*left, 1)),
                    ((*bottom, 1), (*right, 1)),
                    ((*bottom, 0), (*right, 0)),
                ]
            else:
                pairs = [
                    ((*top, 1), (*right, 0)),
                    ((*top, 0), (*right, 1)),
                    ((*left, 1), (*bottom, 0)),
                    ((*left, 0), (*bottom, 1)),
                ]
            for k, (a, b) in enumerate(pairs):
                uf.join(a, b)
                arcs[(j, i, k)] = (a, b)
    return {key: uf.find(a) for key, (a, _) in arcs.items()}


def _colour_paths(
    rng: np.random.Generator,
    paths: Mapping[tuple[int, int, int], Any],
    inks: tuple[int, ...],
    shape: tuple[int, int, int],
    mono: bool,
) -> np.ndarray:
    """Per arc colour table ``(ny, nx, arcs)``: one colour per continuous path."""
    table = np.zeros(shape, dtype=np.uint8)
    roots = sorted({r for r in paths.values()}, key=repr)
    if mono or len(inks) == 1:
        colour = dict.fromkeys(roots, inks[0])
    else:
        w = np.array([0.62, 0.26, 0.12][: len(inks)])
        picks = rng.choice(len(inks), size=len(roots), p=w / w.sum())
        colour = {r: inks[int(p)] for r, p in zip(roots, picks, strict=True)}
    for (j, i, k), r in paths.items():
        table[j, i, k] = colour[r]
    return table


def draw(rng: np.random.Generator, canvas: Canvas, settings: Mapping[str, Any]) -> Plate:
    density = int(settings.get("density", 3))
    mood = str(settings.get("palette_mood", "full"))
    style = weighted(rng, _STYLES)
    target = _TILE.get(density, 50)
    if style == "double":
        target = max(target, 54)
    plate = mount(rng, canvas, settings, bleed_p=0.35, frame_p=0.25)
    s, nx, ny, gx0, gy0 = _grid(plate, target)
    plate.title, plate.note = NAME, _NOTES[style]
    n_states = 4 if style == "quilt" else 2
    sym = weighted(
        rng,
        {"random": 0.35, "mirror": 0.3, "noise": 0.2, "pattern": 0.15}
        if style != "quilt"
        else {"random": 0.15, "mirror": 0.5, "noise": 0.1, "pattern": 0.25},
    )
    states = _states(rng, nx, ny, n_states, sym)

    if style == "filled":
        scheme = pick_scheme(
            rng, mood, grounds={WHITE: 0.5, YELLOW: 0.3, BLACK: 0.2}, n=3, need=LINE
        )
    elif style == "quilt":
        scheme = pick_scheme(
            rng, mood, grounds={WHITE: 0.6, YELLOW: 0.25, BLACK: 0.15}, n=3, need=FILL
        )
    else:
        scheme = pick_scheme(
            rng, mood, grounds={WHITE: 0.6, YELLOW: 0.2, BLACK: 0.12, BLUE: 0.08}, n=3, need=LINE
        )
    ground = scheme.ground
    idx = frame_of(canvas)
    region = plate.box
    xs, ys = pixel_grid(region)
    u, v = xs - gx0, ys - gy0
    ii = np.clip((u // s).astype(np.int64), 0, nx - 1)
    jj = np.clip((v // s).astype(np.int64), 0, ny - 1)
    fx, fy = u - ii * s, v - jj * s
    st = states[jj, ii]
    out = np.full(xs.shape, ground, dtype=np.uint8)
    border = False

    if style in ("arcs", "double", "filled"):
        ax = np.where(st == 0, 0.0, float(s))
        bx = s - ax
        d_a = np.hypot(fx - ax, fy)
        d_b = np.hypot(fx - bx, fy - s)
        if style == "filled":
            c0, c1 = ground, scheme.inks[0]
            corner_par = (ii + jj + st) % 2
            inside = (d_a < s / 2) | (d_b < s / 2)
            # vertex colouring: corner discs take their vertex's colour, the band the other
            out = np.where(inside == (corner_par == 0), c1, c0).astype(np.uint8)
            if len(scheme.inks) > 1 and rng.random() < 0.45:
                pip = scheme.inks[1]
                if contrast(pip, c1) >= FILL and contrast(pip, c0) >= FILL:
                    r_pip = s * float(rng.uniform(0.13, 0.2))
                    which = int(rng.integers(2))
                    near = (d_a < r_pip) | (d_b < r_pip)
                    out[near & (corner_par == which)] = pip
        else:
            w = int(np.clip(round(s * (0.15 if style == "double" else 0.2)), 8, 12))
            radii = (s / 3, 2 * s / 3) if style == "double" else (s / 2,)
            paths = _arc_paths(states, style == "double")
            n_arcs = 2 * len(radii)
            table = _colour_paths(
                rng, paths, scheme.inks, (ny, nx, n_arcs), mono=rng.random() < 0.35
            )
            for k in range(n_arcs):
                d = d_a if k < len(radii) else d_b
                r = radii[k % len(radii)]
                on = np.abs(d - r) < w / 2
                out = np.where(on, table[jj, ii, k], out).astype(np.uint8)
            border = not plate.bleed and rng.random() < 0.4
            if border:
                plate.frame = False
    elif style == "quilt":
        inks = scheme.inks
        cidx = _states(
            rng,
            nx,
            ny,
            min(len(inks), 3),
            "mirror" if sym == "mirror" else "random",
            tiles=False,
        )
        tri = [fx + fy < s, fx > fy, fx + fy > s, fx < fy]  # filled corner TL, TR, BR, BL
        filled = np.select([st == k for k in range(4)], tri)
        colours = np.array(inks, dtype=np.uint8)[cidx[jj, ii] % len(inks)]
        out = np.where(filled, colours, ground).astype(np.uint8)
        border = not plate.bleed and rng.random() < 0.35
    paint(idx, region, out)

    if style == "maze":
        w = int(np.clip(round(s * 0.2), 8, 12))
        mono = rng.random() < 0.5
        lines = []
        for j in range(ny):
            for i in range(nx):
                x, y = gx0 + i * s, gy0 + j * s
                if states[j, i] == 0:
                    lines.append(((x, y), (x + s, y + s)))
                else:
                    lines.append(((x + s, y), (x, y + s)))
        clip = np.zeros_like(idx, dtype=bool)
        x0, y0, x1, y1 = plate.box
        clip[max(0, y0) : min(H, y1), max(0, x0) : min(W, x1)] = True
        accent = scheme.inks[1] if len(scheme.inks) > 1 else scheme.inks[0]
        p_acc = 0.0 if mono else float(rng.uniform(0.12, 0.3))
        flags = rng.random(len(lines)) < p_acc
        for colour, pick in ((scheme.inks[0], ~flags), (accent, flags)):
            chosen = [ln for ln, f in zip(lines, pick, strict=True) if f]
            if not chosen:
                continue
            m = np.zeros_like(clip)
            for a, b in chosen:
                m |= m_line([a, b], w)
            idx[m & clip] = colour
        border = not plate.bleed and rng.random() < 0.3

    if border:
        bw = int(np.clip(round(s * 0.2), 8, 12))
        x0, y0, x1, y1 = plate.box
        bc = scheme.inks[0]
        idx[y0 : y0 + bw // 2, x0:x1] = bc
        idx[y1 - bw // 2 : y1, x0:x1] = bc
        idx[y0:y1, x0 : x0 + bw // 2] = bc
        idx[y0:y1, x1 - bw // 2 : x1] = bc
        plate.frame = False
    blit(canvas, idx)
    return plate
