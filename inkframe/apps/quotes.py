"""Quotes: one quotation per refresh, set like a small letterpress card.

Three layouts of one family (``RenderContext.variant`` forces one; otherwise
the seed, or the hour, picks):

0. **classic**: the quote centred in Playfair Display, balanced lines, a
   short coloured rule, the author in tracked Inter capitals, the source in
   mono.  The rule's colour carries the theme (green nature, blue time and
   science, red otherwise).
1. **poster**: a huge opening quotation mark (yellow, or red for love and
   courage) hanging top left like a drop cap, the italic lines beside it
   indented (verse is stacked under it instead); the author bottom right
   behind a blue rule.
2. **block**: a full-bleed black card, the quote in white Playfair Bold,
   the author in yellow mono.

Sources: the bundled public-domain library (``inkframe/apps/data/quotes.json``,
works offline, filtered by the ``tags`` and ``length`` settings) or ZenQuotes
(``source = "zenquotes"``).  A ZenQuotes card always carries the attribution
their terms require.  When ZenQuotes cannot be reached the card falls back to
the last cached ZenQuotes quote (if it was not shown recently), then to the
bundled library, and says so in one short footer line.
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from PIL import Image, ImageFont

from inkframe import USER_AGENT
from inkframe.apps.base import Field, FontLibrary, RenderContext, schema_defaults
from inkframe.apps.draw import Canvas
from inkframe.apps.quotes_data import (
    TAGS,
    Quote,
    QuoteDataError,
    choose_quote,
    filter_pool,
    load_library,
    load_recent,
    load_zen_cache,
    parse_zenquotes,
    remember,
    save_zen_cache,
    zen_to_quote,
)
from inkframe.apps.quotes_type import (
    draw_lines,
    draw_tracked,
    fit_label,
    fit_quote,
    ink_box,
    split_balanced,
    vmetrics,
)
from inkframe.display.palette import BLACK, BLUE, GREEN, RED, WHITE, YELLOW

log = logging.getLogger("inkframe.apps.quotes")

ZENQUOTES_URL = "https://zenquotes.io/api/random"
#: required on every card that shows a ZenQuotes quote (their terms of use)
ZEN_ATTRIBUTION = "Quotes by ZenQuotes.io"
#: a ZenQuotes quote fetched less than this long ago is reused, not refetched,
#: so a preview and the panel refresh that follows show the same quote
ZEN_REUSE = timedelta(seconds=90)

VARIANT_NAMES: tuple[str, ...] = ("classic", "poster", "block")
#: U+201C LEFT DOUBLE QUOTATION MARK, the poster's hanging mark
OPEN_QUOTE = "“"
CLASSIC, POSTER, BLOCK = range(3)

W, H = 600, 400
#: reserved at the bottom for the one-line status / attribution footer
FOOTER_RESERVE = 30
FOOTER_BASELINE = 382

SETTINGS_SCHEMA: tuple[Field, ...] = (
    Field(
        "source",
        "Source",
        "select",
        "local",
        options=(
            ("local", "Bundled collection (works offline)"),
            ("zenquotes", "ZenQuotes (online)"),
        ),
        help="ZenQuotes needs the internet; the bundled collection fills in when it is "
        "unreachable.",
    ),
    Field(
        "tags",
        "Themes",
        "list",
        [],
        help="One per line, any of: " + ", ".join(TAGS) + ". Empty shows every theme. "
        "Applies to the bundled collection.",
        placeholder="stoic\nnature",
    ),
    Field(
        "length",
        "Length",
        "select",
        "any",
        options=(
            ("short", "Short (up to 90 characters)"),
            ("any", "Any length"),
            ("long", "Long (120 characters or more)"),
        ),
    ),
    Field("show_source", "Show the work and year", "bool", True),
)


# --- network ----------------------------------------------------------------------------


def fetch_zenquote(
    timeout: float, *, transport: httpx.BaseTransport | None = None
) -> dict[str, str]:
    """One random quote from ZenQuotes as ``{"text", "author"}``.

    Raises ``httpx.HTTPError`` or ``ValueError`` on any failure (including
    their rate-limit notice).  Module level so tests can replace it; the
    per-phase timeouts are capped so a dead network costs at most ~10 s.
    """
    t = max(1.0, min(float(timeout), 6.0))
    with httpx.Client(
        timeout=httpx.Timeout(t, connect=min(t, 4.0)),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
        transport=transport,
    ) as client:
        resp = client.get(ZENQUOTES_URL)
        resp.raise_for_status()
        return parse_zenquotes(resp.json())


# --- choosing what to show ----------------------------------------------------------------


@dataclass
class Selection:
    quote: Quote
    notes: list[str] = field(default_factory=list)
    attribution: str | None = None
    #: True when ``quote`` is the "nothing to show" card, not a real quote
    placeholder: bool = False


LIBRARY_MISSING = Quote(
    "placeholder-library",
    "The quote library is missing.",
    "Inkframe",
    "Restore apps/data/quotes.json or use ZenQuotes",
)


def _seed(ctx: RenderContext) -> int:
    if ctx.seed is not None:
        return int(ctx.seed)
    return int(ctx.now.timestamp() // 60)


def choose_variant(ctx: RenderContext) -> int:
    if ctx.variant is not None:
        return int(ctx.variant) % len(VARIANT_NAMES)
    base = ctx.seed if ctx.seed is not None else ctx.now.hour
    return int(base) % len(VARIANT_NAMES)


def _when(ctx: RenderContext, fetched_at: datetime) -> str:
    """``14:05`` today, ``17 Oct`` before: short enough for the footer."""
    local = fetched_at.astimezone(ctx.now.tzinfo) if ctx.now.tzinfo else fetched_at
    if local.date() == ctx.now.date():
        return f"{local:%H:%M}"
    return f"{local.day} {local:%b}"


def _zen(ctx: RenderContext, recent: list[str]) -> tuple[Quote | None, str | None, str | None]:
    """``(quote, note, reason)``: a ZenQuotes quote (fresh or cached) or
    ``None`` with the reason it is unavailable."""
    cache = load_zen_cache(ctx.cache_dir)
    if cache is not None and timedelta(0) <= ctx.now - cache.fetched_at < ZEN_REUSE:
        return cache.quote, None, None
    if ctx.online:
        try:
            raw = fetch_zenquote(ctx.http_timeout)
            quote = zen_to_quote(raw)
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as e:
            log.warning("ZenQuotes fetch failed: %s", e)
            reason = "ZenQuotes unreachable"
        else:
            save_zen_cache(ctx.cache_dir, raw, ctx.now)
            return quote, None, None
    else:
        reason = "Offline"
    if cache is not None and cache.quote.id not in recent:
        # short: it shares the footer line with the attribution
        return cache.quote, f"{reason} · cached {_when(ctx, cache.fetched_at)}", None
    return None, None, reason


def select(ctx: RenderContext, settings: dict) -> Selection:
    """Pick the quote for this render.  May fetch (and cache) a ZenQuotes
    quote; never writes ``recent.json`` (``QuotesApp.render`` does, after
    drawing)."""
    recent = load_recent(ctx.cache_dir)
    reason = None
    if settings["source"] == "zenquotes":
        quote, note, reason = _zen(ctx, recent)
        if quote is not None:
            return Selection(quote, [note] if note else [], ZEN_ATTRIBUTION)
    try:
        library = load_library()
    except QuoteDataError as e:
        log.error("quotes: %s", e)
        notes = [f"{reason} · no quotes available"] if reason else []
        return Selection(LIBRARY_MISSING, notes, None, placeholder=True)
    pool, notes = filter_pool(library, settings["tags"], settings["length"])
    if reason:
        notes.insert(0, f"{reason} · showing a local quote")
    quote = choose_quote(pool, recent, random.Random(_seed(ctx)))
    return Selection(quote, notes)


# --- the footer ----------------------------------------------------------------------


def _footer(
    c: Canvas,
    fonts: FontLibrary,
    note: str | None,
    attribution: str | None,
    fg: int,
) -> None:
    """One mono line at the bottom: a status note on the left (after a small
    red alert square), the required attribution on the right."""
    if not note and not attribution:
        return
    right = W - 32
    if attribution:
        f, t, text = fit_label(fonts, attribution, 240, "mono", "regular", 14, 0.0)
        box = draw_tracked(c, right, FOOTER_BASELINE, text, f, fg, tracking=t, align="right")
        right = box[0] - 16
    if note:
        cap = vmetrics(fonts.font("mono", 14))[0]
        sq = 8
        top = FOOTER_BASELINE - cap // 2 - sq // 2 - 1
        c.rect((32, top, 32 + sq - 1, top + sq - 1), fill=RED)
        f, t, text = fit_label(fonts, note, right - 48, "mono", "regular", 14, 0.0)
        draw_tracked(c, 48, FOOTER_BASELINE, text, f, fg, tracking=t)


# --- the three layouts --------------------------------------------------------------------


def _theme_rule(quote: Quote) -> int:
    tags = set(quote.tags)
    if "nature" in tags:
        return GREEN
    if tags & {"science", "time"}:
        return BLUE
    return RED


def _serif(fonts: FontLibrary, weight: str) -> Callable[[int], ImageFont.FreeTypeFont]:
    return lambda s: fonts.font("serif", s, weight)


def _classic(c: Canvas, fonts: FontLibrary, quote: Quote, show_source: bool, footer: bool):
    top, bottom = 30, H - 30 - (FOOTER_RESERVE if footer else 0)
    author_f, author_t, author = fit_label(
        fonts, quote.author.upper(), 520, "sans", "medium", 22, 3.5, min_size=16
    )
    a_cap = vmetrics(author_f)[0]
    source = quote.source if show_source else ""
    if source:
        source_f, _, source = fit_label(fonts, source, 520, "mono", "regular", 14, 0.0)
        s_cap, s_desc = vmetrics(source_f)
    gap_rule, rule_h, gap_author, gap_source = 24, 4, 20, 13
    meta = gap_rule + rule_h + gap_author + a_cap + (gap_source + s_cap + s_desc if source else 0)
    fit = fit_quote(
        quote.text,
        _serif(fonts, "regular"),
        520,
        min(260, bottom - top - meta),
        max_size=76,
        leading=1.22,
    )
    y = top + (bottom - top - fit.height - meta) // 2
    draw_lines(c, fit, W / 2, y, BLACK, "ms")
    ry = y + fit.height + gap_rule
    c.rect((W // 2 - 32, ry, W // 2 + 31, ry + rule_h - 1), fill=_theme_rule(quote))
    ab = ry + rule_h + gap_author + a_cap
    draw_tracked(c, W / 2, ab, author, author_f, BLACK, tracking=author_t, align="center")
    if source:
        draw_tracked(c, W / 2, ab + gap_source + s_cap, source, source_f, BLACK, align="center")


def _poster(c: Canvas, fonts: FontLibrary, quote: Quote, show_source: bool, footer: bool):
    tags = set(quote.tags)
    mark_colour = RED if tags & {"love", "courage"} else YELLOW
    # a hanging quotation mark, set like a drop cap: its ink top level with
    # the ascenders of the first line, the lines beside it indented
    mark_f = fonts.font("serif", 460, "bold")
    ink = ink_box(mark_f, OPEN_QUOTE)
    mx, top = 30, 34
    mark_w, mark_h = ink[2] - ink[0], ink[3] - ink[1]

    bottom = H - 28 - (FOOTER_RESERVE if footer else 0)
    author_f, author_t, author = fit_label(
        fonts, quote.author.upper(), 440, "sans", "semibold", 20, 2.5, min_size=15
    )
    a_cap = vmetrics(author_f)[0]
    source = quote.source if show_source else ""
    if source:
        source_f, _, source = fit_label(fonts, source, 500, "mono", "regular", 14, 0.0)
        s_cap, s_desc = vmetrics(source_f)
        source_base = bottom - s_desc
        author_base = source_base - s_cap - 12
    else:
        author_base = bottom
    author_top = author_base - a_cap

    left, right = 36, W - 32
    indent = mx + mark_w + 24

    def beside_mark(leading: int) -> int:
        """How many lines start level with the mark (and are indented)."""
        return math.ceil((mark_h + 12) / leading)

    def widths(leading: int) -> Callable[[int], int]:
        n = beside_mark(leading)
        return lambda i: (right - indent) if i < n else (right - left)

    avail = author_top - 30 - top
    italic = _serif(fonts, "italic")
    # two arrangements: lines running beside the mark (a drop), or the mark
    # above a full-width block (stack, better for verse with long lines);
    # whichever sets the quote larger wins.  The 52 px cap keeps the mark
    # about two lines deep, so it reads as a drop cap, not a stray blob.
    drop = fit_quote(quote.text, italic, widths, avail, max_size=52, leading=1.24)
    gap = 20
    stack = fit_quote(
        quote.text, italic, right - left, avail - mark_h - gap, max_size=52, leading=1.24
    )
    stacked = stack.size > drop.size
    if not stacked and len(drop.lines) == 1 and drop.height + drop.leading <= avail:
        # one short line beside a two-line-deep mark reads as a missing line
        drop.lines = split_balanced(drop.lines[0], drop.font)
    group_h = mark_h + gap + stack.height if stacked else max(mark_h, drop.height)
    # a short quote leaves no dead band: the group sits a little above the
    # middle of the space it has
    y = top + int(max(0, avail - group_h) * 0.4)
    c.text((mx - ink[0], y - ink[1]), OPEN_QUOTE, mark_f, mark_colour)
    if stacked:
        draw_lines(c, stack, left, y + mark_h + gap, BLACK, "ls")
    else:
        n = beside_mark(drop.leading)
        draw_lines(c, drop, left, y, BLACK, "ls", x_at=lambda i: indent if i < n else left)

    box = draw_tracked(
        c, right, author_base, author, author_f, BLACK, tracking=author_t, align="right"
    )
    rule_y = author_base - a_cap // 2 - 2
    c.rect((box[0] - 18 - 56, rule_y, box[0] - 18 - 1, rule_y + 3), fill=BLUE)
    if source:
        draw_tracked(c, right, source_base, source, source_f, BLACK, align="right")


def _block(c: Canvas, fonts: FontLibrary, quote: Quote, show_source: bool, footer: bool):
    c.rect((0, 0, W - 1, H - 1), fill=BLACK)
    left = 40
    bottom = H - 30 - (FOOTER_RESERVE if footer else 0)
    author_f, author_t, author = fit_label(
        fonts, quote.author.upper(), 520, "mono", "bold", 18, 1.5, min_size=14
    )
    a_cap = vmetrics(author_f)[0]
    source = quote.source if show_source else ""
    if source:
        source_f, _, source = fit_label(fonts, source, 520, "mono", "regular", 14, 0.0)
        s_cap, s_desc = vmetrics(source_f)
        source_base = bottom - s_desc
        author_base = source_base - s_cap - 12
    else:
        author_base = bottom
    rule_top = author_base - a_cap - 18 - 4

    area_top, area_bottom = 38, rule_top - 26
    fit = fit_quote(
        quote.text,
        _serif(fonts, "bold"),
        520,
        area_bottom - area_top,
        max_size=72,
        leading=1.18,
    )
    y = area_top + (area_bottom - area_top - fit.height) // 2
    draw_lines(c, fit, left, y, WHITE, "ls")
    c.rect((left, rule_top, left + 39, rule_top + 3), fill=YELLOW)
    draw_tracked(c, left, author_base, author, author_f, YELLOW, tracking=author_t)
    if source:
        draw_tracked(c, left, source_base, source, source_f, WHITE)


_LAYOUTS = (_classic, _poster, _block)


def compose(
    fonts: FontLibrary,
    quote: Quote,
    variant: int,
    *,
    show_source: bool = True,
    note: str | None = None,
    attribution: str | None = None,
) -> Canvas:
    """Draw one card.  Pure: the same inputs give the same pixels."""
    variant = int(variant) % len(_LAYOUTS)
    c = Canvas(background=WHITE)
    footer = bool(note or attribution)
    _LAYOUTS[variant](c, fonts, quote, show_source, footer)
    _footer(c, fonts, note, attribution, WHITE if variant == BLOCK else BLACK)
    return c


# --- the app ----------------------------------------------------------------------------------


class QuotesApp:
    id = "quotes"
    name = "Quotes"
    description = (
        "A quotation set like a printed card: bundled public-domain classics or ZenQuotes."
    )
    needs_dither = False
    settings_schema = SETTINGS_SCHEMA
    variants = len(VARIANT_NAMES)
    variant_names = VARIANT_NAMES

    def render(self, ctx: RenderContext) -> Image.Image:
        settings = {**schema_defaults(SETTINGS_SCHEMA), **(ctx.settings or {})}
        sel = select(ctx, settings)
        variant = CLASSIC if sel.placeholder else choose_variant(ctx)
        card = compose(
            ctx.fonts,
            sel.quote,
            variant,
            show_source=bool(settings["show_source"]),
            note=sel.notes[0] if sel.notes else None,
            attribution=sel.attribution,
        )
        if not sel.placeholder:
            remember(ctx.cache_dir, load_recent(ctx.cache_dir), sel.quote.id, ctx.now)
        return card.to_image()


APP = QuotesApp()
