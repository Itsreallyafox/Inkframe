"""Typesetting helpers for the Headlines app: faces, wrapping, fitting, tracking.

Everything is laid out on baselines (Pillow anchor ``ls``), not on the font's
ascender box, so blocks in different fonts align to the same grid.  Drawing
goes through ``Canvas.text`` (crisp, thresholded, palette colours only).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator, Sequence

from PIL import ImageFont

from inkframe.apps.draw import Canvas

_DIGIT_RUN = re.compile(r"\d+(?:[.,:]\d+)*")


def cap_height(font: ImageFont.FreeTypeFont) -> int:
    """Height of a capital H above the baseline, in pixels."""
    return -int(font.getbbox("H", anchor="ls")[1])


class Face:
    """A font for headline text, optionally with lining digits from a second font.

    Playfair Display only has old-style figures ("190" reads as "igo"), so the
    serif face sets digit runs in Inter Bold scaled to Playfair's cap height.
    """

    def __init__(
        self, font: ImageFont.FreeTypeFont, digits: ImageFont.FreeTypeFont | None = None
    ) -> None:
        self.font = font
        self.digits = digits
        self.size = int(font.size)
        self.cap = cap_height(font)
        self._words: dict[str, float] = {}
        self._space = font.getlength(" ")

    def runs(self, s: str) -> list[tuple[str, ImageFont.FreeTypeFont]]:
        if self.digits is None or not any(ch.isdigit() for ch in s):
            return [(s, self.font)]
        out: list[tuple[str, ImageFont.FreeTypeFont]] = []
        pos = 0
        for m in _DIGIT_RUN.finditer(s):
            if m.start() > pos:
                out.append((s[pos : m.start()], self.font))
            out.append((m.group(), self.digits))
            pos = m.end()
        if pos < len(s):
            out.append((s[pos:], self.font))
        return out

    def exact_width(self, s: str) -> int:
        return int(math.ceil(sum(f.getlength(t) for t, f in self.runs(s))))

    def word_width(self, word: str) -> float:
        w = self._words.get(word)
        if w is None:
            w = sum(f.getlength(t) for t, f in self.runs(word))
            self._words[word] = w
        return w

    def width(self, s: str) -> int:
        """Width of ``s`` as the sum of its words and spaces, memoised per word.

        ``getlength`` costs about a millisecond per call and wrapping measures
        every candidate line, so lines are measured word by word; kerning across
        a space is ignored (it is zero or negative in these fonts)."""
        words = s.split(" ")
        return int(
            math.ceil(sum(self.word_width(w) for w in words if w) + self._space * (len(words) - 1))
        )

    def draw(
        self, c: Canvas, x: float, baseline: float, s: str, fill: int, align: str = "left"
    ) -> int:
        w = self.width(s)
        if align == "right":
            x -= w
        elif align == "center":
            x -= w / 2
        for text, font in self.runs(s):
            c.text((x, baseline), text, font, fill, anchor="ls")
            x += font.getlength(text)
        return w


def wrap(text: str, face: Face, width: int, indent: int = 0, indented: int = 0) -> list[str]:
    """Greedy word wrap; a word wider than its line is split.

    The first ``indented`` lines are ``indent`` pixels narrower (text running
    beside a drop numeral)."""
    lines: list[str] = []
    line = ""

    def limit() -> int:
        return width - indent if len(lines) < indented else width

    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if face.width(candidate) <= limit():
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ""
        while face.width(word) > limit() and len(word) > 1:
            cut = len(word) - 1
            while cut > 1 and face.width(word[:cut]) > limit():
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    if line:
        lines.append(line)
    return lines


def clamp(
    lines: Sequence[str], max_lines: int, face: Face, width: int, ellipsis: str = "…"
) -> list[str]:
    """Keep ``max_lines`` lines; end the last one on a whole word plus an ellipsis."""
    if len(lines) <= max_lines:
        return list(lines)
    kept = list(lines[:max_lines])
    words = kept[-1].split()
    while len(words) > 1 and face.width(" ".join(words) + ellipsis) > width:
        words.pop()
    last = " ".join(words).rstrip(" ,;:-–—")
    while last and face.width(last + ellipsis) > width:
        last = last[:-1]
    kept[-1] = last + ellipsis
    return kept


def balanced(text: str, face: Face, width: int) -> list[str]:
    """Wrap into the fewest lines, then narrow the measure as far as possible
    without adding a line, so a headline's lines come out evenly long instead of
    leaving one word dangling on the last line."""
    lines = wrap(text, face, width)
    n = len(lines)
    if n < 2:
        return lines
    lo, hi = int(width * 0.5), width
    while lo < hi:
        mid = (lo + hi) // 2
        if len(wrap(text, face, mid)) <= n:
            hi = mid
        else:
            lo = mid + 1
    return wrap(text, face, lo)


_TAIL = re.compile(r"(?:,|\s[-–—])\s+([^,–—]{3,60})$")
_SPEECH = re.compile(
    r"\b(?:says?|said|tells?|told|warns?|claims?|reports?|according|admits?|insists?|"
    r"confirms?|announces?|argues?|suggests?)\b",
    re.IGNORECASE,
)


def short_forms(title: str) -> list[str]:
    """The title, then (if it has one) the title without a trailing attribution
    clause such as ", Zelensky adviser tells BBC" or ", police say": what a
    sub-editor cuts first when a headline does not fit."""
    forms = [title]
    m = _TAIL.search(title)
    if m and m.start() >= 24 and len(m.group(1).split()) <= 6 and _SPEECH.search(m.group(1)):
        forms.append(title[: m.start()].rstrip(" ,;:"))
    return forms


def set_headline(
    text: str, face: Face, width: int, max_lines: int, indent: int = 0, indented: int = 0
) -> list[str]:
    """Lines for a headline in at most ``max_lines`` lines: the full title if it
    fits, else its short form, else the short form cut with an ellipsis.
    Unindented multi-line headlines are balanced."""
    forms = short_forms(text)
    for form in forms:
        lines = wrap(form, face, width, indent, indented)
        if len(lines) <= max_lines:
            return balanced(form, face, width) if not (indent and indented) else lines
    lines = wrap(forms[-1], face, width, indent, indented)
    last_width = width - indent if max_lines <= indented else width
    return clamp(lines, max_lines, face, last_width)


def lead_options(
    text: str,
    make_face: Callable[[int], Face],
    width: int,
    plans: Sequence[tuple[int, Sequence[int]]],
) -> Iterator[tuple[Face, list[str]]]:
    """Settings of a lead headline, most prominent first.

    ``plans`` is a list of ``(max_lines, sizes)``.  The full title is tried at
    every plan before its short form is; each size at which a form fits in
    ``max_lines`` lines is yielded once.  The last option is always the
    smallest size with the text clamped, so a caller gets at least one."""
    forms = short_forms(text)
    for form in forms:
        seen: set[int] = set()
        for max_lines, sizes in plans:
            for size in sizes:
                if size in seen:
                    continue
                face = make_face(size)
                if len(wrap(form, face, width)) <= max_lines:
                    seen.add(size)
                    yield face, balanced(form, face, width)
    max_lines, sizes = plans[-1]
    face = make_face(sizes[-1])
    yield face, clamp(wrap(forms[-1], face, width), max_lines, face, width)


def fit_lead(
    text: str,
    make_face: Callable[[int], Face],
    width: int,
    plans: Sequence[tuple[int, Sequence[int]]],
) -> tuple[Face, list[str]]:
    """The most prominent setting from ``lead_options``."""
    return next(iter(lead_options(text, make_face, width, plans)))


def tracked(
    c: Canvas,
    x: float,
    baseline: float,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: int,
    tracking: float,
    align: str = "left",
) -> int:
    """Letter-spaced caps (labels, the masthead); returns the width."""
    widths = [font.getlength(ch) for ch in text]
    total = sum(widths) + tracking * max(0, len(text) - 1)
    if align == "right":
        x -= total
    for ch, w in zip(text, widths, strict=True):
        if not ch.isspace():
            c.text((x, baseline), ch, font, fill, anchor="ls")
        x += w + tracking
    return int(math.ceil(total))


def tracked_width(text: str, font: ImageFont.FreeTypeFont, tracking: float) -> int:
    return int(math.ceil(font.getlength(text) + tracking * max(0, len(text) - 1)))


def draw_lines(
    c: Canvas,
    x: float,
    first_baseline: float,
    lines: Sequence[str],
    face: Face,
    fill: int,
    leading: float,
    indent: int = 0,
    indented: int = 0,
) -> float:
    """Draw ``lines`` on a baseline grid (the first ``indented`` lines shifted
    right by ``indent``); returns the last baseline."""
    b = first_baseline
    for i, line in enumerate(lines):
        b = first_baseline + i * leading
        face.draw(c, x + (indent if i < indented else 0), b, line, fill)
    return b
