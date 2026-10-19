"""Headlines: top stories from RSS/Atom feeds, set like a printed front page.

Three layouts of the same family (``RenderContext.variant`` forces one; else
``seed % 3``, else the hour of the render ``% 3``):

0. **editorial**: one lead in Playfair Display Bold, its source and age in
   mono, a black rule, then a short list in Inter Medium with red source tags
   hung in the left margin.
1. **ticker grid**: two columns of numbered stories, each numeral black on a
   yellow square; an odd count leaves a colophon cell (sources, update time).
2. **front page**: a black masthead band with the date, a blue-marked lead in
   Inter Bold and two secondary stories side by side.

When there is nothing to show the card says why in one line (offline with no
cache, no feeds configured, feeds unreachable, nothing recent).  Stale data
after a failed fetch is shown with a red "OFFLINE · CACHED 13:50" note.

Data handling (fetching, cache, story selection) lives in
``inkframe.apps.headlines_data``, headline normalisation in ``headlines_text``,
typesetting in ``headlines_type`` and the drawing in ``headlines_layouts``.
``fetch_feed`` is re-exported here and is the hook tests replace.
"""

from __future__ import annotations

import logging

from PIL import Image

from inkframe.apps import headlines_data as data
from inkframe.apps.base import Field, RenderContext, schema_defaults
from inkframe.apps.draw import Canvas
from inkframe.apps.headlines_data import FeedState, fetch_feed, relative_time
from inkframe.apps.headlines_layouts import (
    LAYOUTS,
    VARIANT_NAMES,
    Look,
    Story,
    layout_empty,
)
from inkframe.display.palette import WHITE

__all__ = [
    "APP",
    "DEFAULT_FEEDS",
    "SETTINGS_SCHEMA",
    "VARIANT_NAMES",
    "HeadlinesApp",
    "fetch_feed",
    "pick_variant",
]

log = logging.getLogger("inkframe.apps.headlines")

#: the most one render waits for the network (feeds are fetched in parallel)
MAX_WAIT = 9.5

DEFAULT_FEEDS: tuple[str, ...] = (
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    "https://www.theguardian.com/world/rss",
)

SETTINGS_SCHEMA: tuple[Field, ...] = (
    Field(
        "feeds",
        "Feeds",
        "list",
        list(DEFAULT_FEEDS),
        help=(
            "One RSS or Atom URL per line. Put a short name before the URL to label it, "
            "e.g. 'BBC https://feeds.bbci.co.uk/news/rss.xml'."
        ),
        placeholder="https://example.com/rss.xml",
        required=True,
    ),
    Field(
        "count",
        "Headlines",
        "number",
        5,
        min=3,
        max=8,
        step=1,
        help="Most stories on one card; each layout shows as many as fit legibly.",
    ),
    Field(
        "max_age_hours",
        "Max age (hours)",
        "number",
        36,
        min=1,
        max=72,
        step=1,
        help="Leave out stories older than this.",
    ),
    Field(
        "mix",
        "Order",
        "select",
        "interleave",
        options=(
            ("interleave", "Interleave feeds, newest first"),
            ("by_feed", "Feed by feed, in list order"),
        ),
        help="Interleave takes one story from each feed in turn.",
    ),
)

# --- the app --------------------------------------------------------------------------


def pick_variant(ctx: RenderContext, variants: int = len(LAYOUTS)) -> int:
    """``ctx.variant`` if set, else ``ctx.seed``, else the hour of the render, mod ``variants``."""
    if ctx.variant is not None:
        return int(ctx.variant) % variants
    if ctx.seed is not None:
        return int(ctx.seed) % variants
    return ctx.now.hour % variants


def empty_message(state: FeedState, max_age_hours: float) -> tuple[str, str]:
    """``(title, one-line reason)`` for the card shown when no story is left."""
    if state.reason == data.NO_FEEDS:
        return "No feeds yet", state.reason
    if state.reason and state.reason != "offline":
        return "Nothing on the wire", state.reason
    if state.stale:
        return "Nothing on the wire", data.OFFLINE_EMPTY
    if state.entries:
        return "A quiet news day", f"No stories in the last {max_age_hours:g} h"
    return "A quiet news day", "The feeds have no stories right now"


class HeadlinesApp:
    id = "headlines"
    name = "Headlines"
    description = "Top stories from your RSS/Atom feeds, set like a printed front page."
    needs_dither = False
    settings_schema = SETTINGS_SCHEMA
    variants = len(LAYOUTS)

    def render(self, ctx: RenderContext) -> Image.Image:
        settings = {**schema_defaults(self.settings_schema), **(ctx.settings or {})}
        now = ctx.now if ctx.now.tzinfo is not None else ctx.now.astimezone()
        feeds = data.parse_feed_lines(settings["feeds"])
        try:
            state = data.load(
                feeds,
                now=now,
                cache_dir=ctx.cache_dir,
                online=ctx.online,
                timeout=min(float(ctx.http_timeout), MAX_WAIT),
                fetch=fetch_feed,  # looked up at call time: tests patch this module's name
            )
        except Exception:  # noqa: BLE001 - the card must render whatever happens
            log.exception("headlines: loading feeds failed")
            state = FeedState(reason=data.UNREACHABLE)
        picked = data.select(
            state.entries,
            [f.url for f in feeds],
            count=int(settings["count"]),
            max_age_hours=float(settings["max_age_hours"]),
            mix=str(settings["mix"]),
            now=now,
            stale=state.stale,
        )
        stories = [Story(e.title, e.source, relative_time(e.published, now)) for e in picked]
        canvas = Canvas(background=WHITE)
        look = Look(ctx)
        if not stories:
            title, reason = empty_message(state, float(settings["max_age_hours"]))
            layout_empty(canvas, look, title, reason, state, now)
        else:
            LAYOUTS[pick_variant(ctx)](canvas, look, stories, state, now)
        return canvas.to_image()


APP = HeadlinesApp()
