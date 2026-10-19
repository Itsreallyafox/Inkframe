"""Layouts of the Headlines app: three variants and the "nothing to show" card.

All drawing is in the six palette colours through ``Canvas``; type is set on
baselines with the helpers in ``headlines_type``.  Each layout takes the
canvas, a ``Look`` (font access), the stories to show (already selected and
normalised), the ``FeedState`` (for the offline note and source list) and the
local time of the render.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from PIL import ImageFont

from inkframe.apps.base import RenderContext
from inkframe.apps.draw import Canvas
from inkframe.apps.headlines_data import FeedState
from inkframe.apps.headlines_type import (
    Face,
    cap_height,
    clamp,
    draw_lines,
    fit_lead,
    lead_options,
    set_headline,
    tracked,
    wrap,
)
from inkframe.display.palette import BLACK, BLUE, RED, WHITE, YELLOW

W, H = 600, 400
#: side margin; every layout aligns to it
M = 28
#: the top margin: the first row of type has its cap line here
TOP = 24
#: nothing is drawn below this line (except full-bleed rules)
BOTTOM = H - 24

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


# --- shared pieces --------------------------------------------------------------------


@dataclass(frozen=True)
class Story:
    title: str
    source: str
    ago: str


@dataclass(frozen=True)
class Look:
    """Font access for one render (``FontLibrary`` caches the fonts)."""

    ctx: RenderContext
    #: faces by (role, size, weight); a face memoises its word widths
    _faces: dict[tuple[str, int, str], Face] = field(default_factory=dict, compare=False)

    def font(self, role: str, size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
        return self.ctx.fonts.font(role, size, weight)

    def face(self, role: str, size: int, weight: str = "regular") -> Face:
        key = (role, size, weight)
        face = self._faces.get(key)
        if face is None:
            face = self._faces[key] = Face(self.font(role, size, weight))
        return face

    def serif(self, size: int) -> Face:
        """Playfair Bold with lining digits from Inter Bold at the same cap height."""
        key = ("serif+digits", size, "bold")
        face = self._faces.get(key)
        if face is None:
            ratio = cap_height(self.font("serif", 100, "bold")) / cap_height(
                self.font("sans", 100, "bold")
            )
            digits = self.font("sans", max(8, round(size * ratio)), "bold")
            face = self._faces[key] = Face(self.font("serif", size, "bold"), digits)
        return face


def date_line(now: datetime) -> str:
    """``MON 19 OCT · 19:05``"""
    day = f"{_DAYS[now.weekday()][:3]} {now.day} {_MONTHS[now.month - 1][:3]}".upper()
    return f"{day} · {now:%H:%M}"


def status_line(state: FeedState, now: datetime) -> str:
    """``OFFLINE · CACHED 13:50`` for stale data, else ``""``."""
    if not state.stale or state.fetched_at is None:
        return ""
    t = state.fetched_at.astimezone(now.tzinfo)
    when = f"{t:%H:%M}"
    if t.date() != now.date():
        when = f"{t.day} {_MONTHS[t.month - 1][:3].upper()} {when}"
    return f"OFFLINE · CACHED {when}"


def meta(
    c: Canvas,
    x: float,
    baseline: float,
    story: Story,
    bold: ImageFont.FreeTypeFont,
    regular: ImageFont.FreeTypeFont,
    accent: int,
    sep: str = ", ",
    max_width: int | None = None,
) -> int:
    """``SOURCE, 2 h ago``: the source in bold accent, the age in black.

    Within ``max_width`` the age loses its "ago" first, then the source is cut."""
    src = story.source.upper()
    ago = story.ago
    rest = f"{sep}{ago}" if ago else ""
    if max_width is not None:
        if c.text_width(src + rest, bold) > max_width and ago.endswith(" ago"):
            rest = f"{sep}{ago[:-4]}"
        src = fit_label(c, src, bold, max_width - c.text_width(rest, regular))
    w = c.text_width(src, bold)
    c.text((x, baseline), src, bold, accent, anchor="ls")
    if rest:
        c.text((x + w, baseline), rest, regular, BLACK, anchor="ls")
        w += c.text_width(rest, regular)
    return w


def fit_label(c: Canvas, text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    """``text`` cut (with a full stop, like an abbreviation) to fit ``width``."""
    if c.text_width(text, font) <= width:
        return text
    while len(text) > 1 and c.text_width(text + ".", font) > width:
        text = text[:-1].rstrip()
    return text + "."


def hline(c: Canvas, y: float, x0: float = M, x1: float = W - M, width: int = 1, fill=BLACK):
    c.rect((int(x0), int(y), int(x1) - 1, int(y) + width - 1), fill=fill)


def vline(c: Canvas, x: float, y0: float, y1: float, width: int = 1, fill=BLACK):
    c.rect((int(x), int(y0), int(x) + width - 1, int(y1) - 1), fill=fill)


# --- variant 0: editorial -------------------------------------------------------------

#: lead sizes: two lines as big as possible, else three
_EDITORIAL_PLANS = ((2, range(52, 39, -2)), (3, range(44, 33, -2)))
#: with at most one story under it the lead may grow
_EDITORIAL_SPARSE = ((2, range(64, 39, -2)), (3, range(56, 33, -2)))


def layout_editorial(c: Canvas, look: Look, stories: Sequence[Story], state: FeedState, now):
    mono, mono_b = look.font("mono", 15), look.font("mono", 15, "bold")
    tag_b, tag_r = look.font("mono", 14, "bold"), look.font("mono", 14)

    # kicker row: the app's name in red, the date on the right
    kick = TOP + cap_height(mono_b)
    tracked(c, M, kick, "HEADLINES", mono_b, RED, 2)
    c.text((W - M, kick), date_line(now), mono, BLACK, anchor="rs")

    # the list below the rule: source tags hang in a margin column as wide as
    # the widest tag (within limits), headlines in Inter Medium beside them
    body = look.face("sans", 22, "medium")
    lead_px, tag_drop, gap = 27, 18, 12
    rest = list(stories[1:5])
    tag_w = max(
        [c.text_width(s.source.upper(), tag_b) for s in rest]
        + [c.text_width(s.ago, tag_r) for s in rest]
        + [64]
    )
    col = M + min(tag_w, 124) + 14
    width = W - M - col
    lists = [set_headline(s.title, body, width, 2) for s in rest]

    def item_h(n: int) -> int:
        return max(body.cap + (n - 1) * lead_px, body.cap + tag_drop) + 6

    def frame(face: Face, lines: list[str]) -> tuple[float, float, float, float]:
        first = kick + 20 + face.cap
        last = first + (len(lines) - 1) * round(face.size * 1.08)
        meta_base = last + round(face.size * 0.25) + 20
        rule = meta_base + 12
        return first, meta_base, rule, rule + 2 + 16

    # the biggest lead that still leaves room for three list items (a sparse
    # card lets the lead grow further)
    want = min(3, len(rest))
    natural = [len(x) for x in lists]
    plans = _EDITORIAL_PLANS if len(rest) >= 2 else _EDITORIAL_SPARSE
    chosen = None
    for face, lines in lead_options(stories[0].title, look.serif, W - 2 * M, plans):
        top = frame(face, lines)[3]
        plan = _plan_list(natural, item_h, gap, BOTTOM + 6 - top, shrink=False)
        chosen = (face, lines, plan)
        if len(plan) >= want:
            break
    assert chosen is not None  # lead_options always yields
    face, lines, plan = chosen
    first, meta_base, rule, top = frame(face, lines)
    if len(plan) < want:
        plan = _plan_list(natural, item_h, gap, BOTTOM + 6 - top, shrink=True)

    draw_lines(c, M, first, lines, face, BLACK, round(face.size * 1.08))
    note = status_line(state, now)
    right = W - M
    if note:
        right = c.text((W - M, meta_base), note, mono_b, RED, anchor="rs")[0]
    meta(c, M, meta_base, stories[0], mono_b, mono, RED, max_width=right - 20 - M)
    hline(c, rule, width=2)

    y = top
    for story, lines_, n in zip(rest, lists, plan, strict=False):
        base = y + body.cap
        c.text((M, base), fit_label(c, story.source.upper(), tag_b, 124), tag_b, RED, anchor="ls")
        if story.ago:
            c.text((M, base + tag_drop), story.ago, tag_r, BLACK, anchor="ls")
        shown = lines_ if n >= len(lines_) else set_headline(story.title, body, width, n)
        draw_lines(c, col, base, shown, body, BLACK, lead_px)
        y += item_h(n) + gap


def _plan_list(natural: list[int], height, gap: int, avail: float, *, shrink: bool) -> list[int]:
    """Line counts for a vertical list: the longest prefix of items that fits at
    natural length; with ``shrink``, two-liners are cut to one line (last
    first) until at least three items fit."""

    def total(lines: list[int]) -> int:
        return sum(height(n) for n in lines) + gap * max(0, len(lines) - 1)

    k = len(natural)
    while k > 0 and total(natural[:k]) > avail:
        k -= 1
    if not shrink or k >= min(3, len(natural)):
        return natural[:k]
    lines = natural[: min(3, len(natural))]
    while total(lines) > avail and any(n > 1 for n in lines):
        lines[max(i for i, n in enumerate(lines) if n > 1)] = 1
    while len(lines) > 1 and total(lines) > avail:
        lines.pop()
    return lines


# --- variant 1: ticker grid -----------------------------------------------------------

#: rows -> (headline size, leading, max lines); the drop numeral is two lines tall
_GRID_TYPE = {3: (19, 23, 3), 2: (22, 27, 4), 1: (26, 32, 7)}


def layout_grid(c: Canvas, look: Look, stories: Sequence[Story], state: FeedState, now):
    mono = look.font("mono", 15)
    small, small_b = look.font("mono", 14), look.font("mono", 14, "bold")
    title_f = look.font("sans", 20, "bold")

    head = TOP + cap_height(title_f)
    tracked(c, M, head, "HEADLINES", title_f, BLACK, 3)
    c.text((W - M, head), date_line(now), mono, BLACK, anchor="rs")
    rule = head + 12
    hline(c, rule, width=3)

    items = list(stories[:6])
    if status_line(state, now) and len(items) % 2 == 0 and len(items) > 1:
        items.pop()  # stale data: make room for the colophon, which carries the note
    colophon = len(items) % 2 == 1
    cells = len(items) + (1 if colophon else 0)
    rows = max(1, cells // 2)
    top, bottom = rule + 3, BOTTOM + 8
    row_h = (bottom - top) / rows
    gutter = 24
    col_w = (W - 2 * M - gutter) // 2
    size, lead_px, max_lines = _GRID_TYPE[min(rows, 3)]
    body = look.face("sans", size, "semibold")
    sq = 2 * lead_px - 6  # a drop numeral two lines deep
    num_f = look.font("sans", round(sq * 0.56), "bold")
    indent = sq + 10

    for r in range(1, rows):
        y = round(top + r * row_h)
        for colm in range(2):
            x0 = M + colm * (col_w + gutter)
            hline(c, y, x0, x0 + col_w)

    for i in range(cells):
        row, colm = divmod(i, 2)
        x0 = M + colm * (col_w + gutter)
        y0 = round(top + row * row_h) + 14
        y1 = round(top + (row + 1) * row_h)
        if i >= len(items):
            _colophon(c, look, x0, y0, col_w, sq, state, now)
            continue
        story = items[i]
        c.rect((x0, y0, x0 + sq - 1, y0 + sq - 1), fill=YELLOW)
        c.text((x0 + sq / 2, y0 + sq / 2), f"{i + 1:02d}", num_f, BLACK, anchor="mm")
        first = y0 + body.cap
        # as many lines as fit above the source line and the cell's padding
        fit = max(1, min(max_lines, 1 + (y1 - 12 - 20 - first) // lead_px))
        lines = set_headline(story.title, body, col_w, fit, indent=indent, indented=2)
        last = draw_lines(c, x0, first, lines, body, BLACK, lead_px, indent=indent, indented=2)
        mx = x0 + (indent if len(lines) < 2 else 0)
        meta(c, mx, last + 20, story, small_b, small, BLACK, " · ", max_width=x0 + col_w - mx)


def _colophon(c: Canvas, look: Look, x0, y0, col_w, sq, state: FeedState, now) -> None:
    """The spare grid cell: which feeds, when they were fetched, the offline note."""
    small, small_b = look.font("mono", 14), look.font("mono", 14, "bold")
    c.stripes((x0, y0, x0 + sq, y0 + sq), BLACK, WHITE, period=6, duty=0.34, angle=45)
    c.rect((x0, y0, x0 + sq - 1, y0 + sq - 1), outline=BLACK, width=2)
    tx = x0 + sq + 10
    tw = x0 + col_w - tx
    b = y0 + cap_height(small_b)
    c.text((tx, b), "SOURCES", small_b, BLACK, anchor="ls")
    face = look.face("sans", 18, "medium")
    names = " · ".join(state.sources) if state.sources else "-"
    note = status_line(state, now)
    # the note on one line if it fits, else two; the names give way to it
    notes = [note] if c.text_width(note, small_b) <= tw else list(note.partition(" · ")[::2])
    name_lines = 1 if note and len(notes) > 1 else 2
    names_set = clamp(wrap(names, face, tw), name_lines, face, tw)
    last = draw_lines(c, tx, b + 25, names_set, face, BLACK, 22)
    if note:
        for k, line in enumerate(notes):
            c.text((tx, last + 24 + 18 * k), line, small_b, RED, anchor="ls")
    elif state.fetched_at is not None:
        when = state.fetched_at.astimezone(now.tzinfo)
        c.text((tx, last + 24), f"UPDATED {when:%H:%M}", small, BLACK, anchor="ls")


# --- variant 2: front page ------------------------------------------------------------

_FRONT_PLANS = ((2, range(38, 31, -2)), (3, range(34, 27, -2)))


def layout_front(c: Canvas, look: Look, stories: Sequence[Story], state: FeedState, now):
    mono, mono_b = look.font("mono", 15), look.font("mono", 15, "bold")
    small, small_b = look.font("mono", 14), look.font("mono", 14, "bold")

    # the masthead band, full bleed
    band = 80
    c.rect((0, 0, W - 1, band - 1), fill=BLACK)
    mast = look.font("sans", 40, "bold")
    mast_base = (band + cap_height(mast)) // 2
    tracked(c, M - 2, mast_base, "HEADLINES", mast, WHITE, 4)
    date_f = look.font("sans", 15, "bold")
    day = _DAYS[now.weekday()].upper()
    date = f"{now.day} {_MONTHS[now.month - 1].upper()} {now.year}"
    tracked(c, W - M, mast_base - 22, day, date_f, WHITE, 1.5, align="right")
    tracked(c, W - M, mast_base, date, date_f, WHITE, 1.5, align="right")
    hline(c, band + 4, 0, W)

    # the lead, marked with a short blue rule
    lead = stories[0]
    kick = band + 30
    c.rect((M, kick - 10, M + 47, kick - 1), fill=BLUE)
    note = status_line(state, now)
    if note:
        right = c.text((W - M, kick), note, small_b, RED, anchor="rs")
    else:
        when = (state.fetched_at or now).astimezone(now.tzinfo)
        right = c.text((W - M, kick), f"UPDATED {when:%H:%M}", small, BLACK, anchor="rs")
    meta(c, M + 60, kick, lead, mono_b, mono, BLUE, max_width=right[0] - 20 - (M + 60))
    sans_bold = lambda s: look.face("sans", s, "bold")  # noqa: E731
    face, lines = fit_lead(lead.title, sans_bold, W - 2 * M, _FRONT_PLANS)
    leading = round(face.size * 1.12)
    last = draw_lines(c, M, kick + 18 + face.cap, lines, face, BLACK, leading)

    second = list(stories[1:3])
    if not second:
        return
    rule = last + round(face.size * 0.28) + 16
    hline(c, rule)
    top = rule + 18
    gutter = 32
    cols = len(second)
    col_w = (W - 2 * M - gutter * (cols - 1)) // cols
    body = look.face("sans", 20, "semibold")
    lead_px = 25
    if cols == 2:
        vline(c, M + col_w + gutter // 2, rule + 12, BOTTOM + 4)
    for i, story in enumerate(second):
        x = M + i * (col_w + gutter)
        b = top + cap_height(small_b)
        meta(c, x, b, story, small_b, small, BLUE, max_width=col_w)
        first = b + 12 + body.cap
        fit = max(1, 1 + int(BOTTOM - first) // lead_px)
        draw_lines(c, x, first, set_headline(story.title, body, col_w, fit), body, BLACK, lead_px)


# --- nothing to show ------------------------------------------------------------------


def layout_empty(
    c: Canvas, look: Look, title: str, reason: str, state: FeedState, now: datetime
) -> None:
    """The designed card for "nothing to show": a drawn newspaper, a serif
    title, the reason in one red mono line and the configured sources."""
    mono, mono_b = look.font("mono", 15), look.font("mono", 15, "bold")
    kick = TOP + cap_height(mono_b)
    tracked(c, M, kick, "HEADLINES", mono_b, RED, 2)
    c.text((W - M, kick), date_line(now), mono, BLACK, anchor="rs")
    rule = kick + 13
    hline(c, rule, width=2)

    paper = (M + 8, 104, M + 176, 330)
    _newspaper(c, paper)

    # the text block, centred on the drawing
    x = M + 212
    tw = W - M - x
    face, lines = fit_lead(title, look.serif, tw, ((2, range(48, 35, -2)),))
    leading = round(face.size * 1.08)
    reason_face = Face(mono_b)
    reasons = clamp(wrap(reason, reason_face, tw), 2, reason_face, tw)
    hint = look.face("sans", 18, "regular")
    sources = clamp(wrap(" · ".join(state.sources), hint, tw), 2, hint, tw)
    height = face.cap + (len(lines) - 1) * leading + 46 + (len(reasons) - 1) * 21
    if sources:
        height += 14 + 23 * len(sources)
    top = (paper[1] + paper[3] + 8) // 2 - height // 2
    last = draw_lines(c, x, top + face.cap, lines, face, BLACK, leading)
    b = last + 46
    for line in reasons:
        c.text((x, b), line, mono_b, RED, anchor="ls")
        b += 21
    if sources:
        draw_lines(c, x, b + 14, sources, hint, BLACK, 23)


def _newspaper(c: Canvas, box: tuple[int, int, int, int]) -> None:
    """A broadsheet drawn in flat black and white, with a letterpress shadow."""
    x0, y0, x1, y1 = box
    c.rect((x0 + 8, y0 + 8, x1 + 8, y1 + 8), fill=BLACK)
    c.rect((x0, y0, x1, y1), fill=WHITE, outline=BLACK, width=3)
    p = 14
    c.rect((x0 + p, y0 + p, x1 - p, y0 + p + 20), fill=BLACK)  # nameplate
    hline(c, y0 + p + 26, x0 + p, x1 - p + 1)
    c.rect((x0 + p, y0 + p + 36, x1 - p - 18, y0 + p + 47), fill=BLACK)  # headline
    c.rect((x0 + p, y0 + p + 53, x1 - p - 52, y0 + p + 64), fill=BLACK)
    mid = (x0 + x1) // 2
    ty = y0 + p + 76
    c.checker((x0 + p, ty, mid - 5, ty + 52), BLACK, WHITE, size=2)  # photo
    c.stripes((mid + 5, ty, x1 - p, y1 - p), BLACK, WHITE, period=7, duty=0.43)  # columns
    c.stripes((x0 + p, ty + 62, mid - 5, y1 - p), BLACK, WHITE, period=7, duty=0.43)


LAYOUTS = (layout_editorial, layout_grid, layout_front)
VARIANT_NAMES: tuple[str, ...] = ("editorial", "ticker grid", "front page")
