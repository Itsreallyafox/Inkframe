"""Trivia: a fun fact, or a moment from history for today's date.

Three layouts of one family (``RenderContext.variant`` forces one; otherwise
the ``layout`` setting, the seed, or the hour picks):

0. **poster**: "DID YOU KNOW?" in tracked capitals with a coloured rule, the
   fact large in Playfair Display, the category and source small at the
   bottom.
1. **onthisday**: "ON THIS DAY · NOVEMBER 9" and two or three dated events
   from Wikipedia, with the licence line; with bundled data the same frame
   carries "TODAY'S FACT" and one fact.
2. **index**: an index card with a category tab, a red top rule, blue ruled
   lines and the source in mono.

Sources (``source`` setting): the bundled facts
(``inkframe/apps/data/trivia.json``, works offline, filtered by the
``categories`` setting), Wikipedia's "On this day" feed (``onthisday``), or
``mix`` (default): bundled on even hours, Wikipedia on odd ones when the
day's events are available.  Wikipedia's events are fetched once a day and
cached; a card that shows them always carries "Wikipedia · CC BY-SA 4.0".
Offline, or when the feed fails, the bundled facts fill in without comment
(the source line shows the bundled source).  Bundled facts are chosen with a
seed from the date and hour and never repeat one of the last 40 shown.

Failure states are cards, never errors: an empty category filter says so, a
missing library says so.  ``validate(settings)`` (run by the settings API)
refuses an unknown category name up front with a ``ValueError`` naming the
field, so the empty-pool card is only reached by settings that bypassed it.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from inkframe.apps.base import Field, RenderContext, schema_defaults
from inkframe.apps.draw import Canvas
from inkframe.apps.trivia_data import (
    CATEGORIES,
    Event,
    Fact,
    TriviaDataError,
    choose_fact,
    event_fact,
    filter_pool,
    load_library,
    load_recent,
    normalise_category,
    onthisday_events,
    remember,
)
from inkframe.apps.trivia_layouts import VARIANT_NAMES, compose, placeholder
from inkframe.display.palette import WHITE

log = logging.getLogger("inkframe.apps.trivia")

#: how many events the on-this-day list shows at most
EVENTS_SHOWN = 3

SETTINGS_SCHEMA: tuple[Field, ...] = (
    Field(
        "source",
        "Source",
        "select",
        "mix",
        options=(
            ("mix", "Mix: bundled facts and Wikipedia’s “On this day”"),
            ("bundled", "Bundled facts only (works offline)"),
            ("onthisday", "Wikipedia’s “On this day” (online)"),
        ),
        help="Wikipedia needs the internet; the bundled facts fill in when it is unreachable.",
    ),
    Field(
        "categories",
        "Categories",
        "list",
        [],
        help="One per line, any of: " + ", ".join(CATEGORIES) + ". Empty shows every category. "
        "Applies to the bundled facts.",
        placeholder="science\nanimals",
    ),
    Field(
        "show_source",
        "Show the source note",
        "bool",
        True,
        help="The Wikipedia licence line is always shown.",
    ),
    Field(
        "layout",
        "Layout",
        "select",
        "rotate",
        help="Rotate shows a different layout each hour",
        options=(
            ("rotate", "Rotate"),
            ("poster", "Poster: Did you know?"),
            ("onthisday", "On this day: dated events"),
            ("index", "Index card"),
        ),
    ),
)


def validate(settings: Mapping[str, Any]) -> None:
    """Check the settings beyond the schema: every ``categories`` entry must be
    one of ``CATEGORIES`` (case and spacing do not matter).  Raises
    ``ValueError`` naming the field."""
    for name in settings.get("categories") or []:
        if name and name.strip() and normalise_category(name) not in CATEGORIES:
            raise ValueError(
                f"categories: unknown category {name!r}; one of " + ", ".join(CATEGORIES)
            )


@dataclass
class Selection:
    """What one render shows."""

    #: ``bundled``, ``onthisday`` or ``placeholder``
    kind: str
    fact: Fact
    #: the dated events for the list layout (``onthisday`` only)
    events: list[Event] = field(default_factory=list)
    #: a placeholder's second line
    detail: str = ""

    @property
    def placeholder(self) -> bool:
        return self.kind == "placeholder"


def _seed(ctx: RenderContext) -> int:
    """``ctx.seed`` if given, else the date and hour: a preview and the panel
    refresh that follows show the same fact."""
    if ctx.seed is not None:
        return int(ctx.seed)
    return int(f"{ctx.now:%Y%m%d%H}")


def pick_variant(ctx: RenderContext, settings: dict[str, Any]) -> int:
    """``ctx.variant`` wins, then a pinned layout setting, then the seed,
    then the hour (so the rotation changes through the day)."""
    n = len(VARIANT_NAMES)
    if ctx.variant is not None:
        return int(ctx.variant) % n
    layout = settings.get("layout", "rotate")
    if layout in VARIANT_NAMES:
        return VARIANT_NAMES.index(layout)
    if ctx.seed is not None:
        return int(ctx.seed) % n
    return ctx.now.hour % n


def wants_onthisday(ctx: RenderContext, source: str) -> bool:
    """Whether this render would show Wikipedia if the day's events exist."""
    if source == "onthisday":
        return True
    return source == "mix" and ctx.now.hour % 2 == 1


def _placeholder(title: str, detail: str) -> Selection:
    return Selection("placeholder", Fact("placeholder", title, "trivia"), detail=detail)


def select(ctx: RenderContext, settings: dict[str, Any]) -> Selection:
    """Pick what to show.  May fetch (and cache) the day's events; never
    writes ``recent.json`` (``TriviaApp.render`` does, after drawing)."""
    rng = random.Random(_seed(ctx))
    if wants_onthisday(ctx, settings["source"]):
        events = onthisday_events(
            ctx.cache_dir, ctx.now, online=ctx.online, http_timeout=ctx.http_timeout
        )
        if events:
            chosen = rng.sample(events, k=min(EVENTS_SHOWN, len(events)))
            lead = chosen[0]
            chosen.sort(key=lambda e: e.year)
            return Selection("onthisday", event_fact(lead), chosen)
    try:
        library = load_library()
    except TriviaDataError as e:
        log.error("trivia: %s", e)
        return _placeholder("The fact library is missing.", "Restore apps/data/trivia.json")
    pool = filter_pool(library, settings["categories"])
    if not pool:
        wanted = ", ".join(settings["categories"][:3])
        # a shortened list ends in an ellipsis, a complete one in a full stop
        tail = "…" if len(settings["categories"]) > 3 else "."
        return _placeholder(
            f"No facts in those categories: {wanted}{tail}",
            "Check the Categories setting on the Apps page",
        )
    recent = load_recent(ctx.cache_dir)
    return Selection("bundled", choose_fact(pool, recent, rng))


class TriviaApp:
    id = "trivia"
    name = "Trivia"
    description = "A fun fact or a moment from history for today's date"
    needs_dither = False
    settings_schema = SETTINGS_SCHEMA
    variants = len(VARIANT_NAMES)
    variant_names = VARIANT_NAMES
    #: the settings API runs this after the schema check (``ValueError`` -> 422)
    validate = staticmethod(validate)

    def render(self, ctx: RenderContext) -> Image.Image:
        settings = {**schema_defaults(SETTINGS_SCHEMA), **(ctx.settings or {})}
        sel = select(ctx, settings)
        if sel.placeholder:
            c = Canvas(background=WHITE)
            placeholder(c, ctx.fonts, sel.fact.text, sel.detail)
            return c.to_image()
        variant = pick_variant(ctx, settings)
        card = compose(
            ctx.fonts,
            sel.fact,
            variant,
            ctx.now,
            events=sel.events,
            show_source=bool(settings["show_source"]),
        )
        if sel.kind == "bundled":
            remember(ctx.cache_dir, load_recent(ctx.cache_dir), sel.fact.id, ctx.now)
        return card.to_image()


APP = TriviaApp()
