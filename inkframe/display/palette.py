"""The six-colour palette of the Waveshare 4inch e-Paper HAT+ (E) (E Ink Spectra 6).

Vocabulary used throughout Inkframe:

* **palette index**: 0..5 in the order BLACK, WHITE, YELLOW, RED, BLUE, GREEN.
  This is what the dither engine emits, what cached renders store, and what
  app renderers compose with.  It is *our* order, chosen to match the vendor's
  code order so the mapping below is monotonic.
* **panel code**: the controller's 4-bit value for that colour.  The
  controller skips ``0x4`` (orange on the 7-colour Spectra panels) so codes
  are ``0,1,2,3,5,6``.  Verified against the vendor driver
  (``EPD_4in0e.h`` 43-48, ``epd4in0e.py`` 188); the table in the original
  plan was wrong and must not be reintroduced.
* **nominal RGB**: pure primaries, what the vendor driver dithers against.
* **measured RGB**: what the panel actually shows; editable in Settings and
  the thing to dither against for honest previews.

Geometry: Inkframe renders *landscape* ``600 x 400`` frames (``(400, 600)``
arrays, rows first).  The controller's native frame is *portrait* ``400 x 600``
(200 bytes per row, 600 rows, high nibble = left pixel).  ``pack_frame``
rotates like the vendor's ``getbuffer`` (``PIL.Image.rotate(90, expand=True)``,
i.e. counter-clockwise, ``numpy.rot90(k=1)``) so landscape pixel ``(x=599, y=0)``
lands at native ``(0, 0)``.  Whether that is physically upright in the frame
is confirmed on hardware with an asymmetric test card; ``rotation=270`` is the
other mounting.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

BLACK, WHITE, YELLOW, RED, BLUE, GREEN = range(6)
NAMES: tuple[str, ...] = ("black", "white", "yellow", "red", "blue", "green")
N_COLORS = 6

#: panel 4-bit code for each palette index
PANEL_CODES = np.array([0x0, 0x1, 0x2, 0x3, 0x5, 0x6], dtype=np.uint8)
#: inverse lookup, 0xFF marks a code the panel never emits/accepts
CODE_TO_INDEX = np.full(16, 0xFF, dtype=np.uint8)
for _i, _c in enumerate(PANEL_CODES):
    CODE_TO_INDEX[_c] = _i

#: landscape size the application renders to
PANEL_WIDTH, PANEL_HEIGHT = 600, 400
#: the controller's own (portrait) frame
NATIVE_WIDTH, NATIVE_HEIGHT = 400, 600
NATIVE_ROW_BYTES = NATIVE_WIDTH // 2  # 200
FRAME_BYTES = NATIVE_WIDTH * NATIVE_HEIGHT // 2  # 120000

RGB = tuple[int, int, int]

NOMINAL_RGB: tuple[RGB, ...] = (
    (0, 0, 0),
    (255, 255, 255),
    (255, 255, 0),
    (255, 0, 0),
    (0, 0, 255),
    (0, 255, 0),
)

#: Starting guess for what the panel really shows.  Spectra 6 whites are
#: slightly grey, the blue is dark and desaturated, the yellow warm, the
#: green dull.  Adam tunes these from the palette test card in Phase 7.
MEASURED_DEFAULT_RGB: tuple[RGB, ...] = (
    (24, 24, 26),
    (226, 226, 220),
    (228, 198, 56),
    (186, 48, 40),
    (36, 58, 130),
    (52, 122, 84),
)

VALID_ROTATIONS = (90, 270)


@dataclass(frozen=True)
class Palette:
    """Six RGB triples indexed by palette index.  Immutable; validated on construction."""

    name: str
    rgb: tuple[RGB, ...]

    def __post_init__(self) -> None:
        if len(self.rgb) != N_COLORS:
            raise ValueError(f"palette needs exactly {N_COLORS} colours, got {len(self.rgb)}")
        norm = []
        for i, c in enumerate(self.rgb):
            if len(c) != 3:
                raise ValueError(f"palette entry {i} ({NAMES[i]}) must be an RGB triple, got {c!r}")
            t = tuple(int(v) for v in c)
            if any(v < 0 or v > 255 for v in t):
                raise ValueError(f"palette entry {i} ({NAMES[i]}) out of range 0..255: {t}")
            norm.append(t)
        object.__setattr__(self, "rgb", tuple(norm))

    # --- conversions -----------------------------------------------------
    def as_array(self) -> np.ndarray:
        """``(6, 3)`` uint8 array, palette-index order."""
        return np.array(self.rgb, dtype=np.uint8)

    def flat(self) -> list[int]:
        """``[r0, g0, b0, r1, ...]`` in the layout Pillow ``putpalette`` expects."""
        return [v for c in self.rgb for v in c]

    def as_dict(self) -> dict[str, list[int]]:
        return {NAMES[i]: list(c) for i, c in enumerate(self.rgb)}

    def to_json(self) -> str:
        return json.dumps({"name": self.name, "colors": self.as_dict()})

    @classmethod
    def from_dict(cls, name: str, colors: dict[str, Sequence[int]]) -> Palette:
        missing = [n for n in NAMES if n not in colors]
        if missing:
            raise ValueError(f"palette is missing colours: {', '.join(missing)}")
        return cls(name, tuple(tuple(colors[n]) for n in NAMES))  # type: ignore[arg-type]

    @classmethod
    def from_json(cls, text: str) -> Palette:
        d = json.loads(text)
        return cls.from_dict(d.get("name", "custom"), d["colors"])

    def with_color(self, index: int, rgb: Iterable[int]) -> Palette:
        rgb_list = list(self.rgb)
        rgb_list[index] = tuple(rgb)  # type: ignore[assignment]
        return Palette(self.name, tuple(rgb_list))


NOMINAL = Palette("nominal", NOMINAL_RGB)
MEASURED_DEFAULT = Palette("measured", MEASURED_DEFAULT_RGB)


# --- frame <-> controller bytes ----------------------------------------------


def _check_rotation(rotation: int) -> None:
    if rotation not in VALID_ROTATIONS:
        raise ValueError(f"rotation must be one of {VALID_ROTATIONS}, got {rotation!r}")


def pack_frame(indices: np.ndarray, rotation: int = 90) -> bytes:
    """Convert a landscape ``(400, 600)`` palette-index frame to the 120000
    controller bytes (portrait, two pixels per byte, high nibble first).

    ``rotation=90`` reproduces the vendor driver's ``getbuffer`` orientation;
    ``rotation=270`` is the frame mounted the other way up.
    """
    _check_rotation(rotation)
    a = np.asarray(indices)
    if a.shape != (PANEL_HEIGHT, PANEL_WIDTH):
        raise ValueError(f"expected shape ({PANEL_HEIGHT}, {PANEL_WIDTH}), got {a.shape}")
    if a.dtype != np.uint8:
        a = a.astype(np.uint8)
    if a.size and a.max() > N_COLORS - 1:
        raise ValueError(f"palette index {int(a.max())} out of range 0..{N_COLORS - 1}")
    codes = PANEL_CODES[a]  # (400, 600) uint8 panel codes
    native = np.rot90(codes, 1 if rotation == 90 else -1)  # (600, 400)
    native = np.ascontiguousarray(native)
    packed = (native[:, 0::2] << 4) | native[:, 1::2]  # (600, 200)
    out = packed.astype(np.uint8).tobytes()
    assert len(out) == FRAME_BYTES, len(out)
    return out


def unpack_frame(data: bytes, rotation: int = 90) -> np.ndarray:
    """Inverse of ``pack_frame``: controller bytes -> ``(400, 600)`` palette indices.

    Raises ``ValueError`` on a wrong length or a nibble that is not a valid
    panel code.
    """
    _check_rotation(rotation)
    buf = np.frombuffer(data, dtype=np.uint8)
    if buf.size != FRAME_BYTES:
        raise ValueError(f"expected {FRAME_BYTES} bytes, got {buf.size}")
    packed = buf.reshape(NATIVE_HEIGHT, NATIVE_ROW_BYTES)
    native = np.empty((NATIVE_HEIGHT, NATIVE_WIDTH), dtype=np.uint8)
    native[:, 0::2] = packed >> 4
    native[:, 1::2] = packed & 0x0F
    idx = CODE_TO_INDEX[native]
    if idx.size and idx.max() == 0xFF:
        bad = np.unique(native[idx == 0xFF])
        raise ValueError(f"frame contains invalid panel code(s): {[hex(int(b)) for b in bad]}")
    landscape = np.rot90(idx, -1 if rotation == 90 else 1)  # back to (400, 600)
    return np.ascontiguousarray(landscape)


# --- frame <-> RGB ----------------------------------------------------------


def indices_to_rgb(indices: np.ndarray, palette: Palette = MEASURED_DEFAULT) -> np.ndarray:
    """``(H, W)`` palette indices -> ``(H, W, 3)`` uint8 RGB using ``palette``."""
    a = np.asarray(indices)
    if a.size and a.max() > N_COLORS - 1:
        raise ValueError(f"palette index {int(a.max())} out of range 0..{N_COLORS - 1}")
    return palette.as_array()[a]


def indices_to_image(indices: np.ndarray, palette: Palette = MEASURED_DEFAULT):
    """``(H, W)`` palette indices -> Pillow ``RGB`` image (lazy Pillow import so
    this module stays importable without Pillow)."""
    from PIL import Image

    return Image.fromarray(indices_to_rgb(indices, palette), "RGB")


def rgb_to_indices_exact(rgb: np.ndarray, palette: Palette = NOMINAL) -> np.ndarray:
    """Map an ``(H, W, 3)`` image that uses *only* the palette's colours to
    indices.  For app renderers that compose directly in palette colours
    (``needs_dither = False``).  Raises ``ValueError`` listing the first few
    off-palette colours found, so a renderer bug is loud instead of silently
    speckled.
    """
    a = np.asarray(rgb)
    if a.ndim != 3 or a.shape[2] != 3:
        raise ValueError(f"expected an (H, W, 3) RGB array, got shape {a.shape}")
    a = a.astype(np.uint8, copy=False)
    pal = palette.as_array()
    out = np.full(a.shape[:2], 0xFF, dtype=np.uint8)
    for i in range(N_COLORS):
        out[np.all(a == pal[i], axis=2)] = i
    if out.size and out.max() == 0xFF:
        bad = np.unique(a[out == 0xFF].reshape(-1, 3), axis=0)
        sample = [tuple(int(v) for v in c) for c in bad[:8]]
        raise ValueError(
            f"image contains {len(bad)} colour(s) not in palette '{palette.name}', e.g. {sample}"
        )
    return out
