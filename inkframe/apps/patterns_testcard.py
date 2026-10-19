"""The palette test card for hardware bring-up (plan section 6).

Deterministic (no seed).  Layout, 600 x 400:

* top band: a black arrow in the top-left corner pointing up with the mono
  text ``TOP LEFT``, a small ``UP`` arrow at the top centre (so the mounting
  orientation is unambiguous), and the card title at the top right;
* six swatches, two rows of three, each filled with one exact palette colour
  inside a 2 px black keyline (the white swatch would vanish otherwise) and
  labelled underneath in Inter Medium 18 px: colour name, palette index and
  panel code, then the *measured* RGB the frame currently assumes, as hex;
* bottom band: a type ladder (12, 14, 18, 24 px) so stroke dropout is visible,
  1 px and 2 px black line sets (vertical and horizontal: the controller scans
  in portrait, so the two directions can differ), a 50 % stripes patch and a
  checker patch in black and white.

``SWATCHES`` exposes the swatch boxes so tests can count pixels.
"""

from __future__ import annotations

from inkframe.apps.base import FontLibrary
from inkframe.apps.draw import Canvas
from inkframe.display.palette import BLACK, NAMES, PANEL_CODES, WHITE, Palette

NAME = "Test card"

_COL_X = (18, 210, 402)
_ROW_Y = (46, 204)
_SW_W, _SW_H = 180, 102
_KEY = 2

#: palette index -> half-open swatch box (including its 2 px keyline)
SWATCHES: dict[int, tuple[int, int, int, int]] = {
    i: (
        _COL_X[i % 3],
        _ROW_Y[i // 3],
        _COL_X[i % 3] + _SW_W,
        _ROW_Y[i // 3] + _SW_H,
    )
    for i in range(6)
}
#: palette index -> half-open box of its two label lines
LABELS: dict[int, tuple[int, int, int, int]] = {
    i: (b[0], b[3] + 4, b[2], b[3] + 52) for i, b in SWATCHES.items()
}
#: palette index -> half-open box of the measured-hex line alone
HEX_LABELS: dict[int, tuple[int, int, int, int]] = {
    i: (b[0], b[3] + 27, b[2], b[3] + 52) for i, b in SWATCHES.items()
}
KEYLINE = _KEY
#: orientation marks (half-open boxes): the TOP LEFT arrow and the UP arrow
MARK_TOP_LEFT = (0, 0, 140, 44)
MARK_UP = (288, 0, 336, 40)
#: the bottom band: type ladder, line sets, texture patches (fixed layout)
BAND_TOP, BAND_BOTTOM = 362, 392
LADDER: tuple[tuple[int, int], ...] = ((12, 18), (14, 60), (18, 110), (24, 168))  # size, x
VLINES_X, HLINES_X, HLINES_W = 252, 304, 36
#: texture patch boxes (half-open)
PATCHES: dict[str, tuple[int, int, int, int]] = {
    "stripes": (354, BAND_TOP, 394, BAND_BOTTOM),
    "checker": (404, BAND_TOP, 444, BAND_BOTTOM),
}


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _up_arrow(c: Canvas, x: int, top: int, head_w: int, head_h: int, shaft_w: int, total: int):
    """A solid arrow pointing up; ``x`` is the centre line."""
    half = head_w // 2
    c.polygon([(x, top), (x + half, top + head_h), (x - half, top + head_h)], fill=BLACK)
    s = shaft_w // 2
    c.rect((x - s, top + head_h - 1, x - s + shaft_w - 1, top + total), fill=BLACK)


def draw(fonts: FontLibrary, measured: Palette) -> Canvas:
    c = Canvas(background=WHITE)
    mono_b = fonts.font("mono", 16, "bold")
    mono_14 = fonts.font("mono", 14, "bold")
    label = fonts.font("sans", 18, "medium")

    # --- orientation ------------------------------------------------------------
    _up_arrow(c, 27, 6, head_w=22, head_h=14, shaft_w=8, total=32)
    c.text((46, 30), "TOP LEFT", mono_b, BLACK, anchor="ls")
    _up_arrow(c, 300, 8, head_w=16, head_h=10, shaft_w=4, total=22)
    c.text((312, 30), "UP", mono_14, BLACK, anchor="ls")
    c.text((582, 30), "PALETTE TEST CARD", mono_14, BLACK, anchor="rs")

    # --- swatches ----------------------------------------------------------------
    for i, (x0, y0, x1, y1) in SWATCHES.items():
        c.rect((x0, y0, x1 - 1, y1 - 1), fill=BLACK)
        c.rect((x0 + _KEY, y0 + _KEY, x1 - 1 - _KEY, y1 - 1 - _KEY), fill=i)
        name = NAMES[i].capitalize()
        code = int(PANEL_CODES[i])
        lx, ly = x0, y1 + 6
        c.text((lx, ly), f"{name}  {i} / 0x{code:X}", label, BLACK, anchor="la")
        c.text((lx, ly + 23), _hex(measured.rgb[i]), label, BLACK, anchor="la")

    # --- bottom band: type ladder, line sets, textures ---------------------------
    base = 390
    for size, x in LADDER:
        c.text((x, base), f"{size} Ag", fonts.font("sans", size, "regular"), BLACK, anchor="ls")
    top, bottom = BAND_TOP, BAND_BOTTOM
    x = VLINES_X
    for w in (1, 2):  # vertical 1 px and 2 px lines
        for _ in range(4):
            c.rect((x, top, x + w - 1, bottom - 1), fill=BLACK)
            x += w + 3
        x += 5
    y = top
    for w in (1, 2):  # horizontal 1 px and 2 px lines
        for _ in range(4):
            c.rect((HLINES_X, y, HLINES_X + HLINES_W - 1, y + w - 1), fill=BLACK)
            y += w + 2
        y += 2
    x0, y0, x1, y1 = PATCHES["stripes"]
    c.stripes((x0, y0, x1, y1), BLACK, WHITE, period=2, duty=0.5, angle=0)
    c.rect((x0 - 1, y0 - 1, x1, y1), outline=BLACK, width=1)
    x0, y0, x1, y1 = PATCHES["checker"]
    c.checker((x0, y0, x1, y1), BLACK, WHITE, size=2)
    c.rect((x0 - 1, y0 - 1, x1, y1), outline=BLACK, width=1)
    # the hex lines are what the frame *assumes* the panel shows (Settings > palette)
    c.text((582, base), "hex = measured", fonts.font("mono", 14, "regular"), BLACK, anchor="rs")
    return c
