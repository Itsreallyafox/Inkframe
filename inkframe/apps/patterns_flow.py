"""Flow: 300-800 polylines traced through a seeded vector field.

Fields: ``noise`` (direction = a fractal Perlin value turned into an angle),
``curl`` (perpendicular to the gradient of a Perlin height map, so lines run
along contours and wrap around blobs, like a topographic map) and ``swirl``
(one or two vortices plus noise).  The field is sampled on a 4 px grid and
interpolated bilinearly.

Styles:

* ``contours``: long 2 px lines kept apart by a collision grid (a line stops
  when it would come closer than ``d_sep`` to another), so they pack evenly
  like engraving;
* ``ribbons``:  the same with wide spacing and 4 px strokes;
* ``strokes``:  short 3 px dashes that may overlap, like hatching.

Tracing is vectorised: a batch of seeds advances together, forward and
backward, one numpy step at a time; batches run one after another so early
lines grow long and later ones fill the gaps.  Lines stop at the plate edge,
which always leaves a quiet margin around the field.  Colour follows a
smooth zone map, the line direction, or a weighted random pick.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from inkframe.apps.draw import Canvas
from inkframe.apps.patterns_common import (
    LINE,
    Perlin,
    Plate,
    fill_box,
    mount,
    pick_scheme,
    weighted,
)
from inkframe.display.palette import BLACK, BLUE, WHITE, YELLOW

NAME = "Flow"
_SEEDS = {1: 300, 2: 420, 3: 540, 4: 660, 5: 800}
_STYLES = {"contours": 0.42, "ribbons": 0.28, "strokes": 0.30}
_FIELDS = {"noise": 0.4, "curl": 0.35, "swirl": 0.25}
_GRID = 4.0

Sampler = Callable[[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]


def _field(rng: np.random.Generator, kind: str, box: tuple[int, int, int, int]) -> Sampler:
    """A unit-vector field over ``box``: ``f(x, y) -> (vx, vy)``."""
    x0, y0, x1, y1 = box
    gx = np.arange(x0 - _GRID, x1 + 2 * _GRID, _GRID)
    gy = np.arange(y0 - _GRID, y1 + 2 * _GRID, _GRID)
    X, Y = np.meshgrid(gx, gy)
    if kind == "curl":
        psi = Perlin(rng, period=float(rng.uniform(130, 230)), octaves=2)(X, Y)
        dpy, dpx = np.gradient(psi, _GRID)
        vx, vy = dpy, -dpx
        # rescue flat spots: blend in a weak constant drift
        drift = float(rng.uniform(0, 2 * math.pi))
        eps = 0.08 * float(np.abs(np.hypot(vx, vy)).mean() + 1e-9)
        vx = vx + eps * math.cos(drift)
        vy = vy + eps * math.sin(drift)
    elif kind == "swirl":
        n = int(rng.choice([1, 2]))
        vx = np.zeros_like(X)
        vy = np.zeros_like(X)
        bw, bh = x1 - x0, y1 - y0
        for _ in range(n):
            cx = x0 + bw * float(rng.uniform(0.25, 0.75))
            cy = y0 + bh * float(rng.uniform(0.3, 0.7))
            spin = float(rng.choice([-1, 1]))
            inward = float(rng.uniform(-0.35, 0.35))
            dx, dy = X - cx, Y - cy
            d = np.hypot(dx, dy) + 1e-6
            fall = 1.0 / (1.0 + (d / (0.5 * bh)) ** 2)
            vx += fall * (spin * -dy / d + inward * -dx / d)
            vy += fall * (spin * dx / d + inward * -dy / d)
        wobble = Perlin(rng, period=float(rng.uniform(120, 200)), octaves=2)(X, Y)
        ang = np.arctan2(vy, vx) + wobble * float(rng.uniform(1.0, 2.2))
        vx, vy = np.cos(ang), np.sin(ang)
    else:
        noise = Perlin(rng, period=float(rng.uniform(150, 300)), octaves=2)(X, Y)
        ang = float(rng.uniform(0, 2 * math.pi)) + noise * 2 * math.pi * float(
            rng.uniform(0.7, 1.4)
        )
        vx, vy = np.cos(ang), np.sin(ang)
    norm = np.hypot(vx, vy) + 1e-12
    vx, vy = vx / norm, vy / norm
    gh, gw = vx.shape
    ox, oy = gx[0], gy[0]

    def sample(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        fx = np.clip((x - ox) / _GRID, 0, gw - 1.001)
        fy = np.clip((y - oy) / _GRID, 0, gh - 1.001)
        i = fx.astype(np.int64)
        j = fy.astype(np.int64)
        tx, ty = fx - i, fy - j
        out = []
        for f in (vx, vy):
            a = f[j, i] * (1 - tx) + f[j, i + 1] * tx
            b = f[j + 1, i] * (1 - tx) + f[j + 1, i + 1] * tx
            out.append(a * (1 - ty) + b * ty)
        n = np.hypot(out[0], out[1]) + 1e-12
        return out[0] / n, out[1] / n

    return sample


class _Occupancy:
    """A coarse grid remembering the last point each line left in a cell."""

    def __init__(self, box: tuple[int, int, int, int], d_sep: float) -> None:
        self.x0, self.y0 = box[0], box[1]
        self.cell = d_sep
        self.d2 = d_sep * d_sep
        gw = int((box[2] - box[0]) / d_sep) + 3
        gh = int((box[3] - box[1]) / d_sep) + 3
        self.owner = np.full((gh, gw), -1, dtype=np.int64)
        self.step = np.zeros((gh, gw), dtype=np.int64)
        self.px = np.zeros((gh, gw))
        self.py = np.zeros((gh, gw))
        self.lag = int(math.ceil(3 * d_sep / 2.0)) + 2

    def _cells(self, p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        ci = ((p[:, 0] - self.x0) / self.cell).astype(np.int64) + 1
        cj = ((p[:, 1] - self.y0) / self.cell).astype(np.int64) + 1
        gh, gw = self.owner.shape
        return np.clip(ci, 1, gw - 2), np.clip(cj, 1, gh - 2)

    _DJ = np.array([-1, -1, -1, 0, 0, 0, 1, 1, 1])
    _DI = np.array([-1, 0, 1, -1, 0, 1, -1, 0, 1])

    def blocked(self, p: np.ndarray, ids: np.ndarray, t: int) -> np.ndarray:
        """True where a point comes closer than ``d_sep`` to another line (or to
        an older stretch of its own line)."""
        ci, cj = self._cells(p)
        jj = cj[:, None] + self._DJ
        ii = ci[:, None] + self._DI
        o = self.owner[jj, ii]
        d2 = (self.px[jj, ii] - p[:, :1]) ** 2 + (self.py[jj, ii] - p[:, 1:]) ** 2
        mine = o == ids[:, None]
        other = (o >= 0) & ~mine
        own_old = mine & (np.abs(t - self.step[jj, ii]) > self.lag)
        return ((other | own_old) & (d2 < self.d2)).any(axis=1)

    def claim(self, p: np.ndarray, ids: np.ndarray, t: int) -> None:
        ci, cj = self._cells(p)
        self.owner[cj, ci] = ids
        self.px[cj, ci] = p[:, 0]
        self.py[cj, ci] = p[:, 1]
        self.step[cj, ci] = t


def trace(
    sample: Sampler,
    seeds: np.ndarray,
    box: tuple[int, int, int, int],
    *,
    step: float,
    max_steps: np.ndarray,
    d_sep: float,
    batch: int,
    min_points: int,
    growth: float = 1.0,
) -> list[np.ndarray]:
    """Trace a polyline through each seed (both directions); returns point arrays.

    Seeds run in batches of ``batch`` that grow by ``growth`` each round: a
    few early lines get the whole field to grow long, later batches fill in."""
    x0, y0, x1, y1 = box
    occ = _Occupancy(box, d_sep) if d_sep > 0 else None
    lines: list[np.ndarray] = []
    bounds = []
    b0, size = 0, float(max(1, batch))
    while b0 < len(seeds):
        bounds.append((b0, min(len(seeds), b0 + int(size))))
        b0 += int(size)
        size *= growth
    for b0, b1 in bounds:
        s = seeds[b0:b1]
        n = len(s)
        ids = np.arange(b0, b0 + n)
        budget = np.concatenate([max_steps[b0 : b0 + n], max_steps[b0 : b0 + n]])
        ok = np.ones(n, dtype=bool)
        if occ is not None:
            ok = ~occ.blocked(s, ids, 0)
            # one seed per neighbourhood within the batch too
            ci, cj = occ._cells(s)
            _, first = np.unique(cj * 100000 + ci, return_index=True)
            keep = np.zeros(n, dtype=bool)
            keep[first] = True
            ok &= keep
            occ.claim(s[ok], ids[ok], 0)
        p = np.concatenate([s, s])
        sign = np.concatenate([np.ones(n), -np.ones(n)])
        wid = np.concatenate([ids, ids])
        alive = np.concatenate([ok, ok])
        steps = int(budget.max()) if n else 0
        trail = np.empty((steps + 1, 2 * n, 2))
        trail[0] = p
        count = np.ones(2 * n, dtype=np.int64)
        for t in range(steps):
            vx, vy = sample(p[:, 0], p[:, 1])
            q = p + step * sign[:, None] * np.stack([vx, vy], axis=1)
            alive &= (q[:, 0] >= x0) & (q[:, 0] < x1) & (q[:, 1] >= y0) & (q[:, 1] < y1)
            alive &= t < budget
            if occ is not None and alive.any():
                live = np.nonzero(alive)[0]
                hit = occ.blocked(q[live], wid[live], t + 1)
                alive[live[hit]] = False
            if not alive.any():
                trail = trail[: t + 1]
                break
            p = np.where(alive[:, None], q, p)
            trail[t + 1] = p
            count[alive] = t + 2
            if occ is not None:
                live = np.nonzero(alive)[0]
                occ.claim(q[live], wid[live], t + 1)
        for k in range(n):
            if not ok[k]:
                continue
            fwd = trail[: count[k], k]
            back = trail[: count[n + k], n + k]
            pts = np.concatenate([back[::-1], fwd[1:]])
            if len(pts) >= min_points:
                lines.append(pts)
    return lines


def draw(rng: np.random.Generator, canvas: Canvas, settings: Mapping[str, Any]) -> Plate:
    density = int(settings.get("density", 3))
    mood = str(settings.get("palette_mood", "full"))
    plate = mount(rng, canvas, settings, bleed_p=0.0, frame_p=0.35)
    style = weighted(rng, _STYLES)
    kind = weighted(rng, _FIELDS)
    plate.title, plate.note = NAME, f"{kind} {style}"
    scheme = pick_scheme(
        rng,
        mood,
        grounds={WHITE: 0.68, BLACK: 0.12, YELLOW: 0.1, BLUE: 0.1},
        n=3,
        need=LINE,
        lead_black=0.55,
    )
    box = plate.box
    if scheme.ground != WHITE:
        fill_box(canvas, box, scheme.ground)
    sample = _field(rng, kind, box)
    n = _SEEDS.get(density, 540)
    x0, y0, x1, y1 = box
    seeds = np.stack([rng.uniform(x0 + 1, x1 - 1, n), rng.uniform(y0 + 1, y1 - 1, n)], axis=1)
    if style == "strokes":
        step, width, d_sep = 3.0, 3, 0.0
        max_steps = rng.integers(4, 11, n)
        min_points = 4
    elif style == "ribbons":
        step, width = 2.5, 4
        d_sep = float({1: 18, 2: 16, 3: 14, 4: 13, 5: 12}.get(density, 14))
        max_steps = np.full(n, 150)
        min_points = 22
    else:
        step, width = 2.0, 2
        d_sep = float({1: 12, 2: 10, 3: 9, 4: 8, 5: 7}.get(density, 9))
        max_steps = np.full(n, 170)
        min_points = 18
    lines = trace(
        sample,
        seeds,
        box,
        step=step,
        max_steps=max_steps,
        d_sep=d_sep,
        batch=n if d_sep <= 0 else 10,
        growth=1.0 if d_sep <= 0 else 1.45,
        min_points=min_points,
    )

    inks = scheme.inks
    mode = weighted(rng, {"zones": 0.45, "random": 0.35, "angle": 0.2})
    if len(inks) == 1:
        mode = "mono"
    zone = Perlin(rng, period=float(rng.uniform(160, 260)), octaves=1)
    weights = np.array([0.62, 0.26, 0.12][: len(inks)])
    weights = weights / weights.sum()
    picks = rng.choice(len(inks), size=max(1, len(lines)), p=weights)
    cut = np.cumsum(weights)[:-1]
    for k, pts in enumerate(lines):
        if mode == "zones":
            mid = pts[len(pts) // 2]
            z = float(zone(np.array(mid[0]), np.array(mid[1]))) / 1.2 + 0.5
            ci = int(np.searchsorted(cut, z))
        elif mode == "angle":
            d = pts[-1] - pts[0]
            a = (math.atan2(float(d[1]), float(d[0])) % math.pi) / math.pi
            ci = int(np.searchsorted(cut, a))
        elif mode == "random":
            ci = int(picks[k])
        else:
            ci = 0
        colour = inks[min(ci, len(inks) - 1)]
        canvas.line([(float(x), float(y)) for x, y in pts], fill=colour, width=width)
    if not lines:  # a degenerate field: never leave the plate empty
        canvas.line([(x0, (y0 + y1) / 2), (x1 - 1, (y0 + y1) / 2)], fill=inks[0], width=width)
    return plate
