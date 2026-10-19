"""Typesetting for the Quotes app: fitting, wrapping, letter-spaced labels.

Everything here measures with *advance widths summed per word* (each word
measured once per font, cached).  Measuring every growing candidate line
with ``getlength`` made a long quote cost ~0.5 s on the desktop, which the
Pi Zero 2 W would turn into four seconds; summing cached word widths is
two orders of magnitude cheaper and differs from the real line by at most a
pixel of kerning, which ``SAFETY`` absorbs.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from inkframe.apps.base import FontLibrary
from inkframe.apps.draw import Canvas, rgb

#: below 22 px Playfair's hairlines start to break up on the panel
MIN_QUOTE_SIZE = 22
#: verse keeps its own line breaks down to this size before lines may wrap
VERSE_MIN = 24
#: a slightly lower threshold than Canvas' default keeps Playfair's thin
#: strokes whole when the glyph mask is cut to on/off
SERIF_THRESHOLD = 104
#: px of slack between summed word widths and the measure (kerning, rounding)
SAFETY = 2

#: ``leading -> (line index -> measure in px)``: lets a layout narrow the lines
#: that run beside a graphic (the poster's hanging quotation mark)
Widths = Callable[[int], Callable[[int], int]]


@dataclass
class Fit:
    font: ImageFont.FreeTypeFont
    size: int
    lines: list[str]
    leading: int
    #: first baseline below the block top, and the descent below the last one
    ascent: int
    descent: int

    @property
    def height(self) -> int:
        return self.ascent + (len(self.lines) - 1) * self.leading + self.descent


def vmetrics(font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    """``(ascender height, descender depth)`` of the font's actual glyphs."""
    top = font.getbbox("Hdlk", anchor="ls")[1]
    bottom = font.getbbox("gpqy", anchor="ls")[3]
    return int(-top), int(bottom)


def ink_box(font: ImageFont.FreeTypeFont, text: str) -> tuple[int, int, int, int]:
    """The inked pixels of ``text`` drawn at ``(0, 0)`` with anchor ``la``
    (font boxes include side bearings and the baseline; this does not)."""
    left, top, right, bottom = font.getbbox(text)
    pad = 8
    img = Image.new("L", (right - left + 2 * pad, bottom - top + 2 * pad), 0)
    ImageDraw.Draw(img).text((pad - left, pad - top), text, font=font, fill=255)
    bb = img.getbbox() or (pad, pad, pad, pad)
    dx, dy = left - pad, top - pad
    return (bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy)


@lru_cache(maxsize=16384)
def word_width(font: ImageFont.FreeTypeFont, word: str) -> float:
    return font.getlength(word)


def line_width(font: ImageFont.FreeTypeFont, line: str) -> float:
    words = line.split()
    if not words:
        return 0.0
    return sum(word_width(font, w) for w in words) + word_width(font, " ") * (len(words) - 1)


def wrap_measure(
    text: str, font: ImageFont.FreeTypeFont, measure: Callable[[int], int]
) -> list[str]:
    """Greedy word wrap in which line ``i`` may be ``measure(i)`` px wide.

    ``\\n`` (a verse break) always starts a new line.  Words are never split:
    an overlong word overflows its line and ``overflows`` reports it.
    """
    space = word_width(font, " ")
    lines: list[str] = []
    for para in text.split("\n"):
        words: list[str] = []
        width = 0.0
        for word in para.split():
            w = word_width(font, word)
            candidate = width + space + w if words else w
            if words and candidate + SAFETY > measure(len(lines)):
                lines.append(" ".join(words))
                words, width = [word], w
            else:
                words.append(word)
                width = candidate
        lines.append(" ".join(words))
    return lines


def overflows(
    lines: list[str], font: ImageFont.FreeTypeFont, measure: Callable[[int], int]
) -> bool:
    return any(line_width(font, line) + SAFETY > measure(i) for i, line in enumerate(lines))


def fit_quote(
    text: str,
    make_font: Callable[[int], ImageFont.FreeTypeFont],
    width: int | Widths,
    height: int,
    *,
    max_size: int,
    min_size: int = MIN_QUOTE_SIZE,
    leading: float = 1.2,
) -> Fit:
    """The largest size at which ``text`` fits ``width`` x ``height``.

    * verse (text with ``\\n``) first tries to keep every verse line whole,
      down to ``VERSE_MIN``; only then may a long verse line wrap;
    * prose is then *balanced* (the narrowest measure that keeps the same
      number of lines) and, if the last line is still one lonely word, set
      up to 20 % smaller where that word finds company;
    * at ``min_size`` an overlong text is cut with an ellipsis instead of
      overflowing the box.
    """
    widths: Widths = width if callable(width) else (lambda _lead: lambda _i: width)
    verse_lines = text.count("\n") + 1 if "\n" in text else 0

    def attempt(size: int, scale: float = 1.0) -> tuple[Fit, Callable[[int], int]]:
        f = make_font(size)
        a, d = vmetrics(f)
        lead = int(round(size * leading))
        base = widths(lead)
        measure = base if scale == 1.0 else (lambda i: int(base(i) * scale))
        return Fit(f, size, wrap_measure(text, f, measure), lead, a, d), base

    def fits(fit: Fit, base: Callable[[int], int]) -> bool:
        return fit.height <= height and not overflows(fit.lines, fit.font, base)

    def search(lo: int, hi: int, max_lines: int | None = None) -> Fit | None:
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            fit, base = attempt(mid)
            if fits(fit, base) and (max_lines is None or len(fit.lines) <= max_lines):
                best, lo = fit, mid + 1
            else:
                hi = mid - 1
        return best

    if verse_lines:
        whole = search(max(min_size, VERSE_MIN), max_size, max_lines=verse_lines)
        if whole is not None:
            return whole
    best = search(min_size, max_size)
    if best is None:
        best, base = attempt(min_size)
        room = max(1, (height - best.ascent - best.descent) // best.leading + 1)
        if len(best.lines) > room:
            kept = best.lines[:room]
            last = kept[-1]
            while last and line_width(best.font, last + "…") + SAFETY > base(room - 1):
                last = last[:-1].rstrip()
            kept[-1] = last.rstrip(",;:—") + "…"
            best.lines = kept
        return best

    def balance(fit: Fit) -> Fit:
        n = len(fit.lines)
        lo_s, hi_s = 50, 100  # percent of the measure
        while lo_s < hi_s:
            mid_s = (lo_s + hi_s) // 2
            trial, base = attempt(fit.size, mid_s / 100)
            if len(trial.lines) == n and not overflows(trial.lines, trial.font, base):
                hi_s = mid_s
            else:
                lo_s = mid_s + 1
        fit.lines = attempt(fit.size, hi_s / 100)[0].lines
        return fit

    def widowed(fit: Fit) -> bool:
        return len(fit.lines) >= 2 and len(fit.lines[-1].split()) < 2

    best = balance(best)
    if widowed(best) and not verse_lines:
        # a lonely last word: accept up to 20 % smaller type to avoid it
        for size in range(best.size - 1, max(min_size, int(best.size * 0.8)) - 1, -1):
            fit, base = attempt(size)
            if fits(fit, base):
                fit = balance(fit)
                if not widowed(fit):
                    return fit
    return best


def split_balanced(line: str, font: ImageFont.FreeTypeFont) -> list[str]:
    """``line`` as two lines of the most even width (unchanged if one word)."""
    words = line.split()
    if len(words) < 2:
        return [line]
    best = min(
        range(1, len(words)),
        key=lambda k: max(
            line_width(font, " ".join(words[:k])), line_width(font, " ".join(words[k:]))
        ),
    )
    return [" ".join(words[:best]), " ".join(words[best:])]


def draw_lines(
    c: Canvas,
    fit: Fit,
    x: float,
    top: int,
    fill: int,
    anchor: str,
    x_at: Callable[[int], float] | None = None,
) -> None:
    """Set ``fit``'s lines from ``top`` (the ascender line of the first)."""
    for i, line in enumerate(fit.lines):
        y = top + fit.ascent + i * fit.leading
        lx = x if x_at is None else x_at(i)
        c.text((lx, y), line, fit.font, fill, anchor=anchor, threshold=SERIF_THRESHOLD)


def tracked_width(font: ImageFont.FreeTypeFont, text: str, tracking: float) -> float:
    return sum(word_width(font, ch) for ch in text) + tracking * max(0, len(text) - 1)


def draw_tracked(
    c: Canvas,
    x: float,
    baseline: float,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: int,
    *,
    tracking: float = 0.0,
    align: str = "left",
) -> tuple[int, int, int, int]:
    """Letter-spaced text (for capitals and labels), crisp like ``Canvas.text``.
    Draws into one small mask instead of one full-frame mask per glyph."""
    if not text:
        return (int(x), int(baseline), int(x), int(baseline))
    width = tracked_width(font, text, tracking)
    if align == "center":
        x -= width / 2
    elif align == "right":
        x -= width
    ascent, descent = font.getmetrics()
    pad = 4
    mw, mh = int(math.ceil(width)) + 2 * pad, ascent + descent + 2 * pad
    mask = Image.new("L", (mw, mh), 0)
    draw = ImageDraw.Draw(mask)
    cx = float(pad)
    for ch in text:
        draw.text((cx, pad + ascent), ch, font=font, fill=255, anchor="ls")
        cx += word_width(font, ch) + tracking
    mask = mask.point(lambda v: 255 if v >= 128 else 0)
    ox, oy = int(round(x)) - pad, int(round(baseline)) - ascent - pad
    c.image.paste(rgb(fill), (ox, oy, ox + mw, oy + mh), mask)
    return (ox + pad, oy + pad, ox + pad + int(math.ceil(width)), oy + pad + ascent + descent)


def fit_label(
    fonts: FontLibrary,
    text: str,
    max_width: int,
    role: str,
    weight: str,
    size: int,
    tracking: float,
    min_size: int = 14,
) -> tuple[ImageFont.FreeTypeFont, float, str]:
    """Shrink tracking, then size, until ``text`` fits; finally truncate."""
    for s in range(size, min_size - 1, -1):
        f = fonts.font(role, s, weight)
        for t in (tracking, tracking / 2, 0.0):
            if tracked_width(f, text, t) <= max_width:
                return f, t, text
    f = fonts.font(role, min_size, weight)
    while text and tracked_width(f, text + "…", 0) > max_width:
        text = text[:-1].rstrip()
    return f, 0.0, text + "…"
