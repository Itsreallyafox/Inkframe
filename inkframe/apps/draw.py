"""Six-colour drawing helpers for app renderers.

``Canvas`` wraps a 600 x 400 RGB image whose pixels are *always* exact
``NOMINAL`` palette colours, so ``to_frame()`` never fails and nothing ever
needs dithering.  Colours are palette indices (``BLACK``, ``WHITE``,
``YELLOW``, ``RED``, ``BLUE``, ``GREEN`` from ``inkframe.display.palette``).

Text is the one thing Pillow would anti-alias.  ``Canvas.text`` renders the
glyphs into a mask, thresholds it to on/off and pastes a solid colour through
it, so type stays crisp and never bleeds intermediate colours that the dither
would turn into speckle.  Keep type at 14 px or larger; thin strokes below
that drop out.

Shapes (``rect``, ``ellipse``, ``line``, ``polygon``) are not anti-aliased by
Pillow and are used directly.  ``stripes`` and ``dots`` give flat tone and
texture (the e-ink equivalent of a tint) using only palette colours.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from inkframe.display.palette import (
    N_COLORS,
    NOMINAL,
    PANEL_HEIGHT,
    PANEL_WIDTH,
    WHITE,
    rgb_to_indices_exact,
)

RGB = tuple[int, int, int]
Box = tuple[int, int, int, int]

#: nominal RGB per palette index, the colours a ``Canvas`` draws with
COLORS: tuple[RGB, ...] = NOMINAL.rgb


def rgb(index: int) -> RGB:
    """Nominal RGB of palette index ``index``."""
    if not 0 <= int(index) < N_COLORS:
        raise ValueError(f"palette index out of range: {index!r}")
    return COLORS[int(index)]


class Canvas:
    """A 600 x 400 drawing surface restricted to the six palette colours."""

    def __init__(
        self, background: int = WHITE, width: int = PANEL_WIDTH, height: int = PANEL_HEIGHT
    ) -> None:
        self.width = int(width)
        self.height = int(height)
        self.image = Image.new("RGB", (self.width, self.height), rgb(background))
        self._draw = ImageDraw.Draw(self.image)

    # --- output ------------------------------------------------------------------
    def to_image(self) -> Image.Image:
        return self.image

    def to_frame(self) -> np.ndarray:
        """``(height, width)`` palette indices.  Raises if a non-palette colour
        slipped in (it cannot through this API, but ``paste`` of a foreign image can)."""
        return rgb_to_indices_exact(np.asarray(self.image, dtype=np.uint8), NOMINAL)

    # --- shapes -------------------------------------------------------------------
    def rect(
        self,
        box: Box,
        fill: int | None = None,
        outline: int | None = None,
        width: int = 1,
        radius: int = 0,
    ) -> None:
        kw = {
            "fill": None if fill is None else rgb(fill),
            "outline": None if outline is None else rgb(outline),
            "width": width,
        }
        if radius > 0:
            self._draw.rounded_rectangle(box, radius=radius, **kw)
        else:
            self._draw.rectangle(box, **kw)

    def ellipse(
        self, box: Box, fill: int | None = None, outline: int | None = None, width: int = 1
    ) -> None:
        self._draw.ellipse(
            box,
            fill=None if fill is None else rgb(fill),
            outline=None if outline is None else rgb(outline),
            width=width,
        )

    def line(self, points: Sequence[tuple[float, float]], fill: int, width: int = 1) -> None:
        self._draw.line(list(points), fill=rgb(fill), width=width)

    def polygon(
        self,
        points: Sequence[tuple[float, float]],
        fill: int | None = None,
        outline: int | None = None,
    ) -> None:
        self._draw.polygon(
            list(points),
            fill=None if fill is None else rgb(fill),
            outline=None if outline is None else rgb(outline),
        )

    def arc(self, box: Box, start: float, end: float, fill: int, width: int = 1) -> None:
        self._draw.arc(box, start, end, fill=rgb(fill), width=width)

    def pieslice(self, box: Box, start: float, end: float, fill: int) -> None:
        self._draw.pieslice(box, start, end, fill=rgb(fill))

    # --- textures -------------------------------------------------------------------
    def stripes(
        self, box: Box, a: int, b: int, period: int = 6, duty: float = 0.5, angle: int = 0
    ) -> None:
        """Fill ``box`` with stripes alternating colours ``a`` and ``b``.

        ``angle`` is 0 (horizontal), 90 (vertical) or 45 (diagonal); ``duty``
        is the fraction of each period in colour ``a``.  A flat 50 % tint.
        """
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            return
        ys, xs = np.mgrid[0:h, 0:w]
        if angle == 0:
            t = ys
        elif angle == 90:
            t = xs
        elif angle == 45:
            t = xs + ys
        else:
            raise ValueError("angle must be 0, 45 or 90")
        on = (t % period) < max(1, int(round(period * duty)))
        tile = np.where(on[..., None], np.array(rgb(a), np.uint8), np.array(rgb(b), np.uint8))
        self.image.paste(Image.fromarray(tile.astype(np.uint8), "RGB"), (x0, y0))

    def dots(
        self, box: Box, fill: int, background: int | None = None, spacing: int = 8, radius: int = 2
    ) -> None:
        """A regular dot grid: a halftone-like tone built from solid dots."""
        x0, y0, x1, y1 = box
        if background is not None:
            self.rect(box, fill=background)
        for cy in range(y0 + spacing // 2, y1, spacing):
            for cx in range(x0 + spacing // 2, x1, spacing):
                self.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=fill)

    def checker(self, box: Box, a: int, b: int, size: int = 4) -> None:
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            return
        ys, xs = np.mgrid[0:h, 0:w]
        on = ((xs // size) + (ys // size)) % 2 == 0
        tile = np.where(on[..., None], np.array(rgb(a), np.uint8), np.array(rgb(b), np.uint8))
        self.image.paste(Image.fromarray(tile.astype(np.uint8), "RGB"), (x0, y0))

    # --- text -----------------------------------------------------------------------
    def text(
        self,
        xy: tuple[float, float],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: int,
        anchor: str = "la",
        threshold: int = 128,
    ) -> Box:
        """Draw crisp text; returns its bounding box.  ``anchor`` is Pillow's
        two-letter anchor (``la`` left-ascender, ``ms`` middle-baseline,
        ``ra`` right-ascender, ``mm`` middle-middle ...)."""
        if not text:
            x, y = int(xy[0]), int(xy[1])
            return (x, y, x, y)
        bbox = self._draw.textbbox(xy, text, font=font, anchor=anchor)
        mask = Image.new("L", self.image.size, 0)
        ImageDraw.Draw(mask).text(xy, text, font=font, fill=255, anchor=anchor)
        mask = mask.point(lambda v: 255 if v >= threshold else 0)
        self.image.paste(rgb(fill), (0, 0, self.width, self.height), mask)
        return (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))

    def text_width(self, text: str, font: ImageFont.FreeTypeFont) -> int:
        return int(math.ceil(font.getlength(text)))

    def line_height(self, font: ImageFont.FreeTypeFont, spacing: float = 1.2) -> int:
        ascent, descent = font.getmetrics()
        return int(round((ascent + descent) * spacing))

    def wrap(self, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
        """Greedy word wrap to ``max_width`` pixels; overlong words are split."""
        lines: list[str] = []
        for para in text.replace("\r", "").split("\n"):
            words = para.split()
            if not words:
                lines.append("")
                continue
            line = ""
            for word in words:
                candidate = word if not line else f"{line} {word}"
                if self.text_width(candidate, font) <= max_width:
                    line = candidate
                    continue
                if line:
                    lines.append(line)
                    line = ""
                # the word alone is too wide: split it by characters
                while self.text_width(word, font) > max_width and len(word) > 1:
                    cut = len(word)
                    while cut > 1 and self.text_width(word[:cut], font) > max_width:
                        cut -= 1
                    lines.append(word[:cut])
                    word = word[cut:]
                line = word
            lines.append(line)
        return lines

    def paragraph(
        self,
        xy: tuple[int, int],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: int,
        max_width: int,
        *,
        spacing: float = 1.2,
        max_lines: int | None = None,
        align: str = "left",
        ellipsis: str = "…",
    ) -> int:
        """Wrap and draw ``text`` from ``xy`` (top-left); returns the y below
        the last line.  Truncates to ``max_lines`` with ``ellipsis``."""
        lines = self.wrap(text, font, max_width)
        if max_lines is not None and len(lines) > max_lines:
            lines = lines[:max_lines]
            last = lines[-1]
            while last and self.text_width(last + ellipsis, font) > max_width:
                last = last[:-1].rstrip()
            lines[-1] = (last + ellipsis) if last else ellipsis
        x, y = xy
        lh = self.line_height(font, spacing)
        for line in lines:
            if align == "left":
                self.text((x, y), line, font, fill, anchor="la")
            elif align == "center":
                self.text((x + max_width / 2, y), line, font, fill, anchor="ma")
            elif align == "right":
                self.text((x + max_width, y), line, font, fill, anchor="ra")
            else:
                raise ValueError("align must be left, center or right")
            y += lh
        return y

    def fit_text(
        self,
        text: str,
        max_width: int,
        max_height: int,
        make_font,
        *,
        max_size: int = 96,
        min_size: int = 14,
        spacing: float = 1.15,
    ) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
        """Largest font size (``make_font(size) -> font``) at which ``text``
        wraps into ``max_width`` x ``max_height``.  Returns ``(font, lines,
        size)``; at ``min_size`` the text may still overflow."""
        lo, hi = min_size, max_size
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            f = make_font(mid)
            lines = self.wrap(text, f, max_width)
            if len(lines) * self.line_height(f, spacing) <= max_height:
                best = (f, lines, mid)
                lo = mid + 1
            else:
                hi = mid - 1
        if best is None:
            f = make_font(min_size)
            best = (f, self.wrap(text, f, max_width), min_size)
        return best

    # --- compositing ----------------------------------------------------------------
    def paste(
        self, image: Image.Image, xy: tuple[int, int], mask: Image.Image | None = None
    ) -> None:
        """Paste another exact-palette image (e.g. a pre-dithered asset)."""
        self.image.paste(image.convert("RGB"), xy, mask)
