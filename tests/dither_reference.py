"""Pure-Python reference implementations of the dither engine: the spec in executable form.

Everything here is written for readability, one pixel at a time, and follows
the rules documented in ``inkframe/render/dither.c`` and ``docs/DITHER.md``
independently of the C code:

* channel values are scaled by ``SCALE = 16``;
* the error-adjusted value is clamped to ``[VMIN, VMAX]`` = [-0.5, 1.5] x 255 x 16
  before the nearest-colour search (Inkframe's reading of the plan's "error
  clamping to +-1.5x the channel range"; ``error_diffusion(error_clamp=...)``
  implements the other reading, clamping the incoming error, so the tests can
  show the engine follows this one);
* the pushed error is ``e = (clamped - palette) * q / 256``, where ``q`` is
  ``strength`` converted exactly as C does it; each tap adds ``e * weight``
  to its pixel's accumulator, and a pixel reads ``accumulator / denominator``;
  every division is C's truncating division (``tdiv``);
* nearest-colour ties go to the lower palette index;
* serpentine scans odd rows right-to-left and mirrors the kernel;
* errors falling outside the image are dropped;
* ordered methods add ``(t - 127.5) / 127.5 * strength * spread`` to every
  channel, where the spread is the largest grey-axis offset (at most 96
  levels) that keeps each anchor (palette entry, pure black, pure white) on
  its own nearest colour, found by testing every scaled step (``ordered_spread``).

``srgb_to_lab`` is an independent float64 sRGB -> CIE L*a*b* (D65) conversion
used to check the engine's integer Lab within a tolerance; ``IntLab`` mirrors
the engine's integer Lab pipeline so error diffusion with ``distance="lab"``
can be compared exactly as well.
"""

from __future__ import annotations

import functools
import math

import numpy as np

SCALE = 16
VMAX8 = 255 * SCALE  # 4080
VMIN = -(255 * SCALE) // 2  # -2040
VMAX = (3 * 255 * SCALE) // 2  # 6120
ORDERED_SPREAD = 96  # cap of the palette-tied spread, 8-bit levels
SPREAD_MAX = ORDERED_SPREAD * SCALE  # 1536

#: (denominator, ((dx, dy, weight), ...)) transcribed from the task spec / plan
KERNELS: dict[str, tuple[int, tuple[tuple[int, int, int], ...]]] = {
    "floyd_steinberg": (16, ((1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1))),
    "atkinson": (8, ((1, 0, 1), (2, 0, 1), (-1, 1, 1), (0, 1, 1), (1, 1, 1), (0, 2, 1))),
    "jarvis": (
        48,
        (
            (1, 0, 7), (2, 0, 5),
            (-2, 1, 3), (-1, 1, 5), (0, 1, 7), (1, 1, 5), (2, 1, 3),
            (-2, 2, 1), (-1, 2, 3), (0, 2, 5), (1, 2, 3), (2, 2, 1),
        ),
    ),
    "stucki": (
        42,
        (
            (1, 0, 8), (2, 0, 4),
            (-2, 1, 2), (-1, 1, 4), (0, 1, 8), (1, 1, 4), (2, 1, 2),
            (-2, 2, 1), (-1, 2, 2), (0, 2, 4), (1, 2, 2), (2, 2, 1),
        ),
    ),
    "burkes": (
        32,
        ((1, 0, 8), (2, 0, 4), (-2, 1, 2), (-1, 1, 4), (0, 1, 8), (1, 1, 4), (2, 1, 2)),
    ),
    "sierra": (
        32,
        (
            (1, 0, 5), (2, 0, 3),
            (-2, 1, 2), (-1, 1, 4), (0, 1, 5), (1, 1, 4), (2, 1, 2),
            (-1, 2, 2), (0, 2, 3), (1, 2, 2),
        ),
    ),
    "sierra_two_row": (
        16,
        ((1, 0, 4), (2, 0, 3), (-2, 1, 1), (-1, 1, 2), (0, 1, 3), (1, 1, 2), (2, 1, 1)),
    ),
    "sierra_lite": (4, ((1, 0, 2), (-1, 1, 1), (0, 1, 1))),
}  # fmt: skip


# --- integer helpers ------------------------------------------------------------------


def tdiv(a: int, b: int) -> int:
    """C99 integer division for b > 0: the quotient truncated toward zero."""
    q = abs(a) // b
    return q if a >= 0 else -q


def clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def strength_q8(strength: float) -> int:
    """C's ``q = (int)(clamp(strength, 0.5f, 1.0f) * 256.0f + 0.5f)`` in binary32."""
    s = np.float32(strength)  # ctypes c_float: round to nearest
    s = min(max(s, np.float32(0.5)), np.float32(1.0))
    return int(s * np.float32(256.0) + np.float32(0.5))


def scaled_palette(palette: np.ndarray) -> list[tuple[int, int, int]]:
    return [tuple(int(c) * SCALE for c in p) for p in np.asarray(palette)]


def nearest_rgb(v: tuple[int, int, int], pal: list[tuple[int, int, int]]) -> int:
    """Index of the palette entry with the smallest squared sRGB distance;
    the first (lowest) index wins a tie."""
    best, best_d = 0, None
    for k, p in enumerate(pal):
        d = (v[0] - p[0]) ** 2 + (v[1] - p[1]) ** 2 + (v[2] - p[2]) ** 2
        if best_d is None or d < best_d:
            best, best_d = k, d
    return best


# --- the engine's integer Lab, mirrored -----------------------------------------------


class IntLab:
    """The engine's fixed-point L*a*b* (values x 256), built the same way as dither.c.

    Tables are rounded from float64 here and from C doubles there; the
    rounding-margin test in test_dither.py shows no entry is anywhere near a
    rounding boundary, so the two agree exactly.
    """

    #: sRGB -> XYZ / D65 white, x 4096, rows summing to 4096 (as in dither.c)
    M = ((1777, 1541, 778), (871, 2929, 296), (73, 448, 3575))

    def __init__(self) -> None:
        self.lin = [self._round(65536 * srgb_to_linear(i / 255)) for i in range(256)]
        self.f = [self._round(65536 * lab_f(i / 4096)) for i in range(4097)]

    @staticmethod
    def _round(x: float) -> int:
        return int(math.floor(x + 0.5))

    def lin_of(self, v: int) -> int:
        i, frac = divmod(v, SCALE)  # v >= 0
        if frac == 0:
            return self.lin[i]
        return self.lin[i] + ((self.lin[i + 1] - self.lin[i]) * frac) // SCALE

    def f_of(self, t: int) -> int:
        if t <= 0:
            return self.f[0]
        if t >= 65536:
            return self.f[4096]
        i, frac = divmod(t, 16)
        return self.f[i] + ((self.f[i + 1] - self.f[i]) * frac) // 16

    def lab(self, v: tuple[int, int, int]) -> tuple[int, int, int]:
        """Scaled RGB (each clamped to 0..4080) -> L*, a*, b* x 256."""
        lin = [self.lin_of(clamp(c, 0, VMAX8)) for c in v]
        xyz = [(sum(m * c for m, c in zip(row, lin, strict=True)) + 2048) // 4096 for row in self.M]
        fx, fy, fz = (self.f_of(t) for t in xyz)
        return (
            tdiv(116 * fy - 16 * 65536, 256),
            tdiv(500 * (fx - fy), 256),
            tdiv(200 * (fy - fz), 256),
        )

    def nearest(self, v: tuple[int, int, int], pal_lab: list[tuple[int, int, int]]) -> int:
        lab = self.lab(v)
        best, best_d = 0, None
        for k, p in enumerate(pal_lab):
            d = sum((a - b) ** 2 for a, b in zip(lab, p, strict=True))
            if best_d is None or d < best_d:
                best, best_d = k, d
        return best


_INT_LAB: IntLab | None = None


def int_lab() -> IntLab:
    global _INT_LAB
    if _INT_LAB is None:
        _INT_LAB = IntLab()
    return _INT_LAB


def _chooser(palette: np.ndarray, distance: str):
    pal = scaled_palette(palette)
    if distance == "rgb":
        return pal, lambda v: nearest_rgb(v, pal)
    if distance == "lab":
        il = int_lab()
        pal_lab = [il.lab(p) for p in pal]
        return pal, lambda v: il.nearest(v, pal_lab)
    raise ValueError(distance)


# --- methods ------------------------------------------------------------------------------


def nearest_image(rgb: np.ndarray, palette: np.ndarray, distance: str = "rgb") -> np.ndarray:
    """Method ``none``."""
    return ordered(rgb, palette, None, 1.0, distance)


def error_diffusion(
    rgb: np.ndarray,
    palette: np.ndarray,
    method: str,
    serpentine: bool,
    strength: float,
    distance: str = "rgb",
    *,
    vmin: int = VMIN,
    vmax: int = VMAX,
    error_clamp: int | None = None,
) -> np.ndarray:
    """Error diffusion with kernel ``KERNELS[method]``, full-image error buffer.

    The buffer accumulates ``error * weight``; a pixel divides its sum by the
    kernel denominator (truncating) once, when it is read.

    ``vmin``/``vmax`` and ``error_clamp`` exist only so the tests can show the
    clamp is pinned: with ``error_clamp=E`` the incoming error is clamped to
    ``[-E, E]`` and the value is not clamped (the other reading of the spec).
    """
    denom, taps = KERNELS[method]
    h, w, _ = rgb.shape
    q = strength_q8(strength)
    pal, choose = _chooser(palette, distance)
    err = [[[0, 0, 0] for _ in range(w)] for _ in range(h)]
    out = np.zeros((h, w), dtype=np.uint8)
    for y in range(h):
        reverse = serpentine and y % 2 == 1
        step = -1 if reverse else 1
        xs = range(w - 1, -1, -1) if reverse else range(w)
        for x in xs:
            if error_clamp is None:
                v = tuple(
                    clamp(int(rgb[y, x, c]) * SCALE + tdiv(err[y][x][c], denom), vmin, vmax)
                    for c in range(3)
                )
            else:
                v = tuple(
                    int(rgb[y, x, c]) * SCALE
                    + clamp(tdiv(err[y][x][c], denom), -error_clamp, error_clamp)
                    for c in range(3)
                )
            k = choose(v)
            out[y, x] = k
            e = [tdiv((v[c] - pal[k][c]) * q, 256) for c in range(3)]
            for dx, dy, wt in taps:
                tx, ty = x + step * dx, y + dy
                if 0 <= tx < w and ty < h:  # anything else falls off the image
                    for c in range(3):
                        err[ty][tx][c] += e[c] * wt
    return out


def bayer(n: int) -> list[list[int]]:
    """Raw Bayer matrix 0..n*n-1 by the textbook recursion on nested lists."""
    m = [[0]]
    while len(m) < n:
        s = len(m)
        m = [
            [4 * m[y % s][x % s] + (0, 2, 3, 1)[2 * (y // s) + (x // s)] for x in range(2 * s)]
            for y in range(2 * s)
        ]
    return m


def bayer_thresholds(n: int) -> np.ndarray:
    """Bayer n x n normalised to 0..255: round(v * 255 / (n*n - 1)), half up."""
    nn1 = n * n - 1
    return np.array([[(v * 255 * 2 + nn1) // (2 * nn1) for v in row] for row in bayer(n)], np.uint8)


def ordered_spread(palette: np.ndarray, distance: str = "rgb") -> int:
    """The ordered methods' spread for ``palette`` (scaled units, 0..SPREAD_MAX).

    The largest S such that every anchor (each palette entry, pure black and
    pure white) keeps its nearest colour under every grey-axis offset
    ``(o, o, o)`` with ``|o| <= S``, every scaled step tested.
    """
    key = tuple(tuple(int(c) for c in p) for p in np.asarray(palette))
    return _ordered_spread(key, distance)


@functools.cache
def _ordered_spread(palette: tuple[tuple[int, int, int], ...], distance: str) -> int:
    _, choose = _chooser(np.array(palette, dtype=np.uint8), distance)
    anchors = [tuple(c * SCALE for c in p) for p in palette]
    anchors += [(0, 0, 0), (VMAX8, VMAX8, VMAX8)]
    homes = [choose(a) for a in anchors]
    for o in range(1, SPREAD_MAX + 1):
        for a, home in zip(anchors, homes, strict=True):
            for step in (o, -o):
                if choose(tuple(clamp(c + step, VMIN, VMAX) for c in a)) != home:
                    return o - 1
    return SPREAD_MAX


def ordered(
    rgb: np.ndarray,
    palette: np.ndarray,
    thresholds: np.ndarray | None,
    strength: float,
    distance: str = "rgb",
) -> np.ndarray:
    """Ordered dithering with a tiled square threshold matrix (``None``: method none).

    offset (scaled) = (2t - 255) * q * spread / (255 * 256),
    i.e. (t - 127.5) / 127.5 * strength * spread, spread = ``ordered_spread()``.
    """
    h, w, _ = rgb.shape
    q = strength_q8(strength)
    _, choose = _chooser(palette, distance)
    spread = 0 if thresholds is None else ordered_spread(palette, distance)
    out = np.zeros((h, w), dtype=np.uint8)
    n = 0 if thresholds is None else thresholds.shape[0]
    for y in range(h):
        for x in range(w):
            o = 0
            if thresholds is not None:
                t = int(thresholds[y % n, x % n])
                o = tdiv((2 * t - 255) * q * spread, 255 * 256)
            v = tuple(clamp(int(rgb[y, x, c]) * SCALE + o, VMIN, VMAX) for c in range(3))
            out[y, x] = choose(v)
    return out


# --- float colour science -----------------------------------------------------------------

#: IEC 61966-2-1 sRGB -> XYZ (D65)
SRGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)
D65_WHITE = np.array([0.95047, 1.0, 1.08883])
LAB_EPSILON = 216 / 24389
LAB_KAPPA = 24389 / 27


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lab_f(t: float) -> float:
    return float(np.cbrt(t)) if t > LAB_EPSILON else (LAB_KAPPA * t + 16) / 116


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """``(..., 3)`` 0..255 sRGB -> float64 CIE L*a*b* (D65), vectorised."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    t = (lin @ SRGB_TO_XYZ.T) / D65_WHITE
    f = np.where(t > LAB_EPSILON, np.cbrt(t), (LAB_KAPPA * t + 16) / 116)
    lab = np.empty_like(f)
    lab[..., 0] = 116 * f[..., 1] - 16
    lab[..., 1] = 500 * (f[..., 0] - f[..., 1])
    lab[..., 2] = 200 * (f[..., 1] - f[..., 2])
    return lab
