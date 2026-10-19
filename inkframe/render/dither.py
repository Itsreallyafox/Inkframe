"""Dither engine: ctypes wrapper around the native ``dither.c`` plus the method registry.

The engine maps an ``(H, W, 3)`` uint8 RGB image to ``(H, W)`` uint8 palette
indices with one of twelve methods (``METHODS``).  All per-pixel work happens
in C (``libdither.so`` on the Pi, ``dither.dll`` on the Windows desktop), in
integer arithmetic only, so the output is bit-identical on both machines and
golden hashes can be shared.  See ``docs/DITHER.md`` for the fixed-point design.

There is deliberately **no Python fallback**: a pure-Python error diffusion
takes 10-30 s per frame on the Pi.  If the library has not been built,
``load_library()`` (and so ``dither()``) raises ``DitherLibraryMissing`` whose
message is the build command.  ``load_library()`` also runs the library's
one-time ``dither_init()`` under its lock, so every later call is thread-safe.
ctypes releases the GIL for the duration of the foreign call, so a render on
the worker thread never blocks the event loop.

Manual experiments::

    python -m inkframe.render.dither IN.png OUT.png --method atkinson \\
        --palette measured --strength 0.85 --distance lab [--no-serpentine] [--fit]
"""

from __future__ import annotations

import argparse
import ctypes
import math
import numbers
import sys
import threading
import time
from dataclasses import dataclass
from numbers import Real
from pathlib import Path

import numpy as np

from inkframe.display.palette import (
    MEASURED_DEFAULT,
    NOMINAL,
    PANEL_HEIGHT,
    PANEL_WIDTH,
    Palette,
    indices_to_image,
)

#: must equal ``dither_abi_version()`` in dither.c
ABI_VERSION = 2
#: strength is clamped to this range in C; the wrapper rejects anything outside it
STRENGTH_MIN, STRENGTH_MAX = 0.5, 1.0
DEFAULT_STRENGTH = 0.85
DISTANCES = ("rgb", "lab")
#: limits enforced by dither.c
MAX_DIM = 16384
MIN_COLORS, MAX_COLORS = 2, 16
BLUE_NOISE_SIZE = 64
#: cap of the ordered methods' palette-tied spread, in 8-bit levels (dither.c)
ORDERED_SPREAD_MAX = 96
#: the engine's internal channel units per 8-bit level (dither.c SCALE)
SCALE = 16

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
LIBRARY_PATH = _HERE / ("dither.dll" if sys.platform == "win32" else "libdither.so")
BLUE_NOISE_PATH = _HERE / "blue_noise_64.npy"

#: dither.c return codes -> explanation
_ERRORS = {
    -1: "a required pointer is NULL",
    -2: f"image width/height outside 1..{MAX_DIM}",
    -3: f"palette size outside {MIN_COLORS}..{MAX_COLORS}",
    -4: "unknown method id",
    -5: "distance mode is not 0 (rgb) or 1 (lab)",
    -6: f"blue_noise needs a {BLUE_NOISE_SIZE}x{BLUE_NOISE_SIZE} threshold matrix",
    -7: "strength is NaN",
    -8: "Lab distance used before dither_init()",
    -12: "out of memory",
}


class DitherError(RuntimeError):
    """The native engine failed or is unusable."""


class DitherLibraryMissing(DitherError):
    """``libdither.so`` / ``dither.dll`` has not been built (or cannot be loaded)."""


# --- registry -----------------------------------------------------------------


@dataclass(frozen=True)
class DitherMethod:
    """One entry of the method registry (shown in the UI's method strip)."""

    #: stable id used in recipes, the API and golden-hash keys
    id: str
    #: display name
    name: str
    #: one-line description for the UI
    description: str
    #: "nearest" | "error_diffusion" | "ordered"
    kind: str
    #: the method's value in dither.c's enum
    c_id: int


#: Every method, in the plan's table order (section 6); ``c_id`` is the position.
METHODS: tuple[DitherMethod, ...] = (
    DitherMethod(
        "none",
        "None",
        "No dithering: every pixel takes the nearest palette colour. Best for flat graphics.",
        "nearest",
        0,
    ),
    DitherMethod(
        "floyd_steinberg",
        "Floyd–Steinberg",
        "The classic four-neighbour error diffusion and the vendor default; fine, busy grain.",
        "error_diffusion",
        1,
    ),
    DitherMethod(
        "atkinson",
        "Atkinson",
        "Spreads only 6/8 of the error: lighter, cleaner highlights that suit e-ink.",
        "error_diffusion",
        2,
    ),
    DitherMethod(
        "jarvis",
        "Jarvis–Judice–Ninke",
        "Twelve-neighbour diffusion over three rows: smooth gradients, softer detail.",
        "error_diffusion",
        3,
    ),
    DitherMethod(
        "stucki",
        "Stucki",
        "Twelve neighbours like Jarvis with sharper weights: smooth yet crisp.",
        "error_diffusion",
        4,
    ),
    DitherMethod(
        "burkes",
        "Burkes",
        "Two-row simplification of Stucki: even texture, a little coarser.",
        "error_diffusion",
        5,
    ),
    DitherMethod(
        "sierra",
        "Sierra",
        "Sierra-3: ten neighbours over three rows, close to Jarvis but sharper.",
        "error_diffusion",
        6,
    ),
    DitherMethod(
        "sierra_two_row",
        "Sierra two-row",
        "Seven-neighbour, two-row Sierra: between Sierra-3 and Sierra Lite.",
        "error_diffusion",
        7,
    ),
    DitherMethod(
        "sierra_lite",
        "Sierra Lite",
        "Three-neighbour Sierra: the lightest kernel, close to Floyd–Steinberg.",
        "error_diffusion",
        8,
    ),
    DitherMethod(
        "bayer_4",
        "Bayer 4×4",
        "Ordered 4×4 Bayer matrix: a regular cross-hatch, no error bleed.",
        "ordered",
        9,
    ),
    DitherMethod(
        "bayer_8",
        "Bayer 8×8",
        "Ordered 8×8 Bayer matrix: a finer regular pattern with more tone steps.",
        "ordered",
        10,
    ),
    DitherMethod(
        "blue_noise",
        "Blue noise",
        "Ordered 64×64 blue-noise texture: even, pattern-free grain, no error bleed.",
        "ordered",
        11,
    ),
)
METHODS_BY_ID: dict[str, DitherMethod] = {m.id: m for m in METHODS}
METHOD_IDS: tuple[str, ...] = tuple(m.id for m in METHODS)
KINDS = ("nearest", "error_diffusion", "ordered")


def get_method(method_id: str) -> DitherMethod:
    """The registry entry for ``method_id``; ``ValueError`` listing the valid ids."""
    m = METHODS_BY_ID.get(method_id) if isinstance(method_id, str) else None
    if m is None:
        raise ValueError(f"unknown dither method {method_id!r}; valid: {', '.join(METHOD_IDS)}")
    return m


# --- the native library ---------------------------------------------------------


def build_command() -> str:
    """The command that builds the library on this platform (run from the repo root)."""
    if sys.platform == "win32":
        return "powershell -NoProfile -ExecutionPolicy Bypass -File scripts\\build_dither.ps1"
    return "bash scripts/build_dither.sh"


_lib: ctypes.CDLL | None = None
_lib_lock = threading.Lock()


def load_library() -> ctypes.CDLL:
    """Load and check the native library once (thread-safe); later calls are free.

    Raises ``DitherLibraryMissing`` if the file is missing or cannot be loaded,
    ``DitherError`` if it is a different ABI or disagrees with ``METHODS``.
    Never falls back to Python.
    """
    global _lib
    if _lib is not None:
        return _lib
    with _lib_lock:
        if _lib is not None:
            return _lib
        hint = f"build it from the repo root ({_REPO}) with:  {build_command()}"
        if not LIBRARY_PATH.is_file():
            raise DitherLibraryMissing(
                f"native dither library not built: {LIBRARY_PATH} is missing; {hint}"
            )
        try:
            lib = ctypes.CDLL(str(LIBRARY_PATH))  # CDLL releases the GIL during calls
        except OSError as exc:
            raise DitherLibraryMissing(
                f"cannot load {LIBRARY_PATH} ({exc}); rebuild: {hint}"
            ) from exc
        try:
            lib.dither_abi_version.argtypes = []
            lib.dither_abi_version.restype = ctypes.c_int
            lib.dither_init.argtypes = []
            lib.dither_init.restype = ctypes.c_int
            lib.dither_ordered_spread.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            lib.dither_ordered_spread.restype = ctypes.c_int
            lib.dither_method_name.argtypes = [ctypes.c_int]
            lib.dither_method_name.restype = ctypes.c_char_p
            lib.dither_rgb16_to_lab.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            lib.dither_rgb16_to_lab.restype = ctypes.c_int
            lib.dither.argtypes = [
                ctypes.c_void_p,  # rgb
                ctypes.c_int,  # w
                ctypes.c_int,  # h
                ctypes.c_void_p,  # palette_rgb
                ctypes.c_int,  # n_colors
                ctypes.c_int,  # method
                ctypes.c_int,  # serpentine
                ctypes.c_float,  # strength
                ctypes.c_int,  # distance_mode
                ctypes.c_void_p,  # threshold (or NULL)
                ctypes.c_int,  # threshold_size
                ctypes.c_void_p,  # out_index
            ]
            lib.dither.restype = ctypes.c_int
        except AttributeError as exc:
            raise DitherError(
                f"{LIBRARY_PATH} lacks an expected export ({exc}); rebuild: {hint}"
            ) from exc
        version = lib.dither_abi_version()
        if version != ABI_VERSION:
            raise DitherError(
                f"{LIBRARY_PATH} has ABI version {version}, expected {ABI_VERSION}; rebuild: {hint}"
            )
        for m in METHODS:
            name = lib.dither_method_name(m.c_id)
            if name is None or name.decode("ascii") != m.id:
                raise DitherError(
                    f"{LIBRARY_PATH} names method {m.c_id} {name!r}, registry says {m.id!r}; "
                    f"rebuild: {hint}"
                )
        if lib.dither_method_name(len(METHODS)) is not None:
            raise DitherError(
                f"{LIBRARY_PATH} knows more methods than the registry; rebuild: {hint}"
            )
        # The one-time Lab table build, under the lock and before any other
        # thread can see the library; afterwards the C side only reads it.
        if lib.dither_init() != 0:
            raise DitherError(f"{LIBRARY_PATH}: dither_init() failed")
        _lib = lib
        return lib


# --- threshold matrices -----------------------------------------------------------


def bayer_matrix(n: int) -> np.ndarray:
    """The ``n x n`` Bayer matrix (n a power of two, 2..64) normalised to 0..255.

    Built from the recursive definition ``M(2n) = [[4M, 4M+2], [4M+3, 4M+1]]``
    and scaled as ``round(v * 255 / (n*n - 1))`` (half up), exactly what
    dither.c uses internally for ``bayer_4`` and ``bayer_8``.  ``n`` may be any
    integer type (``int``, ``numpy.int64``, ...), not a bool or a float.
    """
    ok = isinstance(n, numbers.Integral) and not isinstance(n, (bool, np.bool_))
    if not ok or not 2 <= int(n) <= 64 or int(n) & (int(n) - 1):
        raise ValueError(f"Bayer matrix size must be a power of two in 2..64, got {n!r}")
    n = int(n)
    m = np.zeros((1, 1), dtype=np.int64)
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    nn1 = n * n - 1
    return ((m * 510 + nn1) // (2 * nn1)).astype(np.uint8)


_blue_noise: np.ndarray | None = None


def blue_noise_matrix() -> np.ndarray:
    """The bundled 64x64 uint8 blue-noise texture (read-only, loaded once).

    Generated by ``scripts/gen_blue_noise.py`` (void-and-cluster).  A missing,
    unreadable or malformed file raises ``DitherError``.
    """
    global _blue_noise
    if _blue_noise is None:
        try:
            tex = np.load(BLUE_NOISE_PATH, allow_pickle=False)
        except (OSError, ValueError, EOFError) as exc:
            raise DitherError(
                f"cannot read the blue-noise texture {BLUE_NOISE_PATH}: {exc}"
            ) from exc
        if not isinstance(tex, np.ndarray):  # e.g. an .npz archive
            raise DitherError(f"{BLUE_NOISE_PATH} is not a single .npy array")
        if tex.shape != (BLUE_NOISE_SIZE, BLUE_NOISE_SIZE) or tex.dtype != np.uint8:
            raise DitherError(
                f"{BLUE_NOISE_PATH} must be a {BLUE_NOISE_SIZE}x{BLUE_NOISE_SIZE} uint8 array, "
                f"got {tex.shape} {tex.dtype}"
            )
        tex = np.ascontiguousarray(tex)
        tex.setflags(write=False)
        _blue_noise = tex
    return _blue_noise


# --- the entry point ----------------------------------------------------------------


def _palette_array(palette: Palette | np.ndarray) -> np.ndarray:
    if isinstance(palette, Palette):
        return np.ascontiguousarray(palette.as_array())
    if not isinstance(palette, np.ndarray):
        raise ValueError(
            f"palette must be a Palette or an (n, 3) uint8 array, got {type(palette).__name__}"
        )
    if palette.ndim != 2 or palette.shape[1] != 3:
        raise ValueError(f"palette array must have shape (n, 3), got {palette.shape}")
    if not MIN_COLORS <= palette.shape[0] <= MAX_COLORS:
        raise ValueError(
            f"palette must have {MIN_COLORS}..{MAX_COLORS} colours, got {palette.shape[0]}"
        )
    if palette.dtype != np.uint8:
        raise ValueError(f"palette array dtype must be uint8, got {palette.dtype}")
    return np.ascontiguousarray(palette)


def _check_rgb(rgb: np.ndarray) -> np.ndarray:
    if not isinstance(rgb, np.ndarray):
        raise ValueError(f"rgb must be a numpy array, got {type(rgb).__name__}")
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"rgb must have shape (H, W, 3), got {rgb.shape}")
    if rgb.dtype != np.uint8:
        raise ValueError(f"rgb dtype must be uint8, got {rgb.dtype}")
    h, w = rgb.shape[:2]
    if not (1 <= h <= MAX_DIM and 1 <= w <= MAX_DIM):
        raise ValueError(f"rgb height and width must be 1..{MAX_DIM}, got {h} x {w}")
    return np.ascontiguousarray(rgb)


def _check_strength(strength: object) -> float:
    if isinstance(strength, bool) or not isinstance(strength, Real):
        raise ValueError(f"strength must be a number, got {strength!r}")
    s = float(strength)
    if not (math.isfinite(s) and STRENGTH_MIN <= s <= STRENGTH_MAX):
        raise ValueError(
            f"strength must be between {STRENGTH_MIN} and {STRENGTH_MAX}, got {strength!r}"
        )
    return s


def dither(
    rgb: np.ndarray,
    palette: Palette | np.ndarray,
    method: str,
    *,
    serpentine: bool = True,
    strength: float = DEFAULT_STRENGTH,
    distance: str = "lab",
) -> np.ndarray:
    """Dither ``rgb`` (``(H, W, 3)`` uint8) to ``palette``; returns ``(H, W)`` uint8 indices.

    ``method`` is a registry id (``METHOD_IDS``).  ``serpentine`` alternates the
    scan direction per row (error diffusion only).  ``strength`` (0.5..1.0)
    scales the diffused error, or the ordered threshold amplitude as a fraction
    of the palette's spread (``ordered_spread()``); ``none`` ignores it.
    ``distance`` is ``"rgb"`` (Euclidean sRGB) or ``"lab"`` (CIE76).

    Raises ``ValueError`` on bad arguments, ``DitherLibraryMissing`` if the
    native library is not built, ``DitherError`` if the engine fails.
    """
    m = get_method(method)
    if not isinstance(serpentine, (bool, np.bool_)):
        raise ValueError(f"serpentine must be true or false, got {serpentine!r}")
    s = _check_strength(strength)
    if distance not in DISTANCES:
        raise ValueError(f"distance must be one of {', '.join(DISTANCES)}, got {distance!r}")
    pal = _palette_array(palette)
    img = _check_rgb(rgb)
    lib = load_library()

    h, w = img.shape[:2]
    out = np.empty((h, w), dtype=np.uint8)
    thr = blue_noise_matrix() if m.id == "blue_noise" else None
    rc = lib.dither(
        img.ctypes.data,
        w,
        h,
        pal.ctypes.data,
        pal.shape[0],
        m.c_id,
        1 if serpentine else 0,
        s,
        DISTANCES.index(distance),
        None if thr is None else thr.ctypes.data,
        0 if thr is None else thr.shape[0],
        out.ctypes.data,
    )
    if rc != 0:
        raise DitherError(f"dither({m.id}) failed: {_ERRORS.get(rc, 'unknown error')} (code {rc})")
    return out


def ordered_spread(palette: Palette | np.ndarray, distance: str = "lab") -> float:
    """The ordered methods' grey-axis spread for ``palette`` at strength 1.0, in
    8-bit levels (a multiple of 1/16, at most ``ORDERED_SPREAD_MAX``).

    It is the largest offset that keeps every palette colour, pure black and
    pure white on the colour it maps to without dithering, so flat areas of
    those stay flat; the ordered offsets span +-``strength`` times it.  96.0
    (the cap) for the nominal palette; 37.3125 (rgb) and 77.5 (lab) for
    ``MEASURED_DEFAULT``.
    """
    if distance not in DISTANCES:
        raise ValueError(f"distance must be one of {', '.join(DISTANCES)}, got {distance!r}")
    pal = _palette_array(palette)
    rc = load_library().dither_ordered_spread(
        pal.ctypes.data, pal.shape[0], DISTANCES.index(distance)
    )
    if rc < 0:
        raise DitherError(f"dither_ordered_spread failed: {_ERRORS.get(rc, 'unknown error')}")
    return rc / SCALE


def engine_lab(colors: np.ndarray, *, scaled: bool = False) -> np.ndarray:
    """The engine's own integer CIE L*a*b* for ``(..., 3)`` colours, as float64
    L*, a*, b* (the C values divided by 256).  For tests and tuning.

    ``colors`` are 0..255 sRGB values, or with ``scaled=True`` the engine's
    internal units (8-bit value x 16, 4 fractional bits; clamped to 0..4080).
    Either way they must be integers: a float array raises ``ValueError``
    rather than being truncated.
    """
    a = np.asarray(colors)
    if a.ndim < 1 or a.shape[-1] != 3 or a.size == 0:
        raise ValueError(f"colors must have shape (..., 3) and not be empty, got {a.shape}")
    if a.dtype.kind not in "iu":
        raise ValueError(f"colors must be integers, got dtype {a.dtype}")
    c = a.astype(np.int64).reshape(-1, 3)
    if not scaled:
        if c.min() < 0 or c.max() > 255:
            raise ValueError("colors must be 0..255 (pass scaled=True for engine units)")
        c = c * 16
    c = np.ascontiguousarray(np.clip(c, -(2**31), 2**31 - 1).astype(np.int32))
    lab = np.empty(c.shape, dtype=np.int32)
    rc = load_library().dither_rgb16_to_lab(c.ctypes.data, c.shape[0], lab.ctypes.data)
    if rc != 0:
        raise DitherError(f"dither_rgb16_to_lab failed (code {rc})")
    return (lab / 256.0).reshape(a.shape)


# --- CLI ---------------------------------------------------------------------------------

#: Pillow modes holding 16-bit (or wider) integer samples; ``convert("RGB")``
#: would clip them to 255 instead of scaling, so ``_read_rgb`` scales them
_WIDE_INT_MODES = ("I", "I;16", "I;16L", "I;16B", "I;16N")


def _read_rgb(path: Path):
    """Open ``path`` as an upright 8-bit RGB ``PIL.Image``.

    EXIF orientation is applied.  16-bit greyscale (modes ``I;16*`` and ``I``,
    e.g. a 16-bit PNG) is scaled from 0..65535 to 0..255 rather than clipped.
    """
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in _WIDE_INT_MODES:
            wide = np.clip(np.asarray(im).astype(np.int64), 0, 65535)
            grey = ((wide * 255 + 32767) // 65535).astype(np.uint8)
            return Image.fromarray(grey).convert("RGB")
        return im.convert("RGB")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m inkframe.render.dither",
        description="Dither an image to the six-colour panel palette (manual experiments).",
    )
    ap.add_argument("input", type=Path, help="input image (anything Pillow reads)")
    ap.add_argument("output", type=Path, help="output PNG, colourised with the chosen palette")
    ap.add_argument("--method", default="atkinson", choices=METHOD_IDS)
    ap.add_argument("--palette", default="measured", choices=("measured", "nominal"))
    ap.add_argument("--strength", type=float, default=DEFAULT_STRENGTH)
    ap.add_argument("--distance", default="lab", choices=DISTANCES)
    ap.add_argument("--no-serpentine", dest="serpentine", action="store_false")
    ap.add_argument(
        "--fit",
        action="store_true",
        help=f"letterbox into {PANEL_WIDTH}x{PANEL_HEIGHT} on white first (Lanczos)",
    )
    args = ap.parse_args(argv)

    from PIL import Image, ImageOps

    palette = MEASURED_DEFAULT if args.palette == "measured" else NOMINAL
    try:
        img = _read_rgb(args.input)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        # missing, unreadable or not an image (UnidentifiedImageError is an OSError)
        print(f"error: cannot read {args.input}: {exc}", file=sys.stderr)
        return 2
    if args.fit:
        img = ImageOps.pad(
            img, (PANEL_WIDTH, PANEL_HEIGHT), method=Image.Resampling.LANCZOS, color=(255, 255, 255)
        )
    rgb = np.asarray(img, dtype=np.uint8)
    try:
        t0 = time.perf_counter()
        idx = dither(
            rgb,
            palette,
            args.method,
            serpentine=args.serpentine,
            strength=args.strength,
            distance=args.distance,
        )
        ms = (time.perf_counter() - t0) * 1000.0
    except (ValueError, DitherError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        indices_to_image(idx, palette).save(args.output)
    except (OSError, ValueError) as exc:  # bad directory, unknown extension, ...
        print(f"error: cannot write {args.output}: {exc}", file=sys.stderr)
        return 2
    counts = np.bincount(idx.ravel(), minlength=palette.as_array().shape[0])
    print(
        f"{args.method} {rgb.shape[1]}x{rgb.shape[0]} in {ms:.1f} ms -> {args.output} "
        f"(palette {palette.name}, pixels per colour {counts.tolist()})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
