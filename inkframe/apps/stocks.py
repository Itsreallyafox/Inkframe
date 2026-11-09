"""Stocks: prices, day change and a sparkline for the symbols you follow.

Data comes from Yahoo Finance's unofficial chart endpoint through
``stocks_data`` (one request per symbol, all in parallel inside a 9.5 s
budget, a transient failure retried once on the ``query2`` host, our
User-Agent on every request), cached for 15 minutes in
``cache_dir/quotes.json``.  When the refresh fails the cached quotes are
shown with Weather's red "COULDN'T UPDATE, DATA FROM ..." stamp ("OFFLINE,
DATA FROM ..." in offline mode); with nothing cached the card says the
provider is unreachable.  A symbol that failed while the others came through
shows a dash and "no data".  There is no second provider: Stooq, the
fallback the plan named, sits behind a browser challenge (see
``stocks_data``).

Three layouts (``stocks_layouts``): a board of two columns, a hero with the
indices large, and a 4 x 3 grid of tiles; ``layout`` pins one or rotates by
the hour.  ``validate(settings)`` refuses more than twelve symbols, a symbol
with characters Yahoo does not know or a label that is not ``SYMBOL=Name``
with a ``ValueError`` naming the field (the settings API turns it into a 422).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from PIL import Image

from inkframe.apps.base import Field, RenderContext, schema_defaults
from inkframe.apps.draw import Canvas
from inkframe.apps.stocks_data import (
    DOT,
    MAX_SYMBOLS,
    PROVIDER,
    SYMBOL_RE,
    clean_symbols,
    load,
    normalise_symbol,
    parse_label,
    parse_labels,
)
from inkframe.apps.stocks_layouts import LAYOUTS, RENDERERS, Style, render_notice
from inkframe.display.palette import RED, WHITE

log = logging.getLogger("inkframe.apps.stocks")

DEFAULT_SYMBOLS = ["AAPL", "NVDA", "GOOG", "^GSPC", "^IXIC", "^DJI", "T", "F", "META", "AMD"]
DEFAULT_LABELS = ["^GSPC=S&P 500", "^IXIC=Nasdaq", "^DJI=Dow"]

SETTINGS_SCHEMA: tuple[Field, ...] = (
    Field(
        "symbols",
        "Symbols",
        "list",
        DEFAULT_SYMBOLS,
        help=f"One per line, up to {MAX_SYMBOLS}: stocks (AAPL), indices (^GSPC), "
        "crypto (BTC-USD), as Yahoo Finance writes them.",
        placeholder="AAPL\n^GSPC\nBTC-USD",
        required=True,
    ),
    Field(
        "labels",
        "Labels",
        "list",
        DEFAULT_LABELS,
        help="SYMBOL=Name, one per line; the name is shown instead of the symbol.",
        placeholder="^GSPC=S&P 500",
    ),
    Field(
        "range",
        "Sparkline span",
        "select",
        "1mo",
        options=(("1w", "1 week"), ("1mo", "1 month"), ("3mo", "3 months")),
    ),
    Field(
        "show_percent",
        "Show the change in percent",
        "bool",
        True,
        help="Off shows the change in price.",
    ),
    Field(
        "layout",
        "Layout",
        "select",
        "rotate",
        help="Rotate shows a different layout each hour",
        options=(
            ("rotate", "Rotate"),
            ("board", "Board: two columns"),
            ("hero", "Hero: the indices large, the stocks below"),
            ("tiles", "Tiles: a 4 x 3 grid"),
        ),
    ),
)


def validate(settings: Mapping[str, Any]) -> None:
    """Check the settings beyond the schema.  Raises ``ValueError`` naming the field:
    more than ``MAX_SYMBOLS`` symbols, a symbol that is not 1-12 characters from
    ``A-Z 0-9 . ^ - =`` (case does not matter), a symbol listed twice, or a label
    that is not ``SYMBOL=Name``."""
    symbols = settings.get("symbols") or []
    if len(symbols) > MAX_SYMBOLS:
        raise ValueError(f"symbols: at most {MAX_SYMBOLS} symbols, got {len(symbols)}")
    seen: set[str] = set()
    for i, raw in enumerate(symbols, 1):
        sym = normalise_symbol(raw)
        if SYMBOL_RE.fullmatch(sym) is None:
            raise ValueError(
                f"symbols: entry {i} {str(raw).strip()!r} must be 1-12 characters from "
                "A-Z, 0-9, . ^ - = (e.g. AAPL, ^GSPC, BTC-USD)"
            )
        if sym in seen:
            raise ValueError(f"symbols: {sym} is listed twice")
        seen.add(sym)
    for i, raw in enumerate(settings.get("labels") or [], 1):
        parsed = parse_label(raw)
        if parsed is None:
            raise ValueError(
                f"labels: entry {i} {str(raw).strip()!r} must be SYMBOL=Name (e.g. ^GSPC=S&P 500)"
            )
        if SYMBOL_RE.fullmatch(parsed[0]) is None:
            raise ValueError(
                f"labels: entry {i} {str(raw).strip()!r} has a symbol that is not 1-12 "
                "characters from A-Z, 0-9, . ^ - ="
            )


def pick_variant(ctx: RenderContext, settings: Mapping[str, Any]) -> int:
    """``ctx.variant`` wins, then a pinned layout setting, then the seed, then
    the hour (so the rotation changes through the day)."""
    n = len(LAYOUTS)
    if ctx.variant is not None:
        return int(ctx.variant) % n
    layout = settings.get("layout", "rotate")
    if layout in LAYOUTS:
        return LAYOUTS.index(layout)
    if ctx.seed is not None:
        return int(ctx.seed) % n
    return ctx.now.hour % n


class StocksApp:
    id = "stocks"
    name = "Stocks"
    description = "Prices, day change and a one-month sparkline for the symbols you follow"
    needs_dither = False
    settings_schema = SETTINGS_SCHEMA
    variants = len(LAYOUTS)
    variant_names = LAYOUTS
    #: the settings API runs this after the schema check (``ValueError`` -> 422)
    validate = staticmethod(validate)

    def render(self, ctx: RenderContext) -> Image.Image:
        settings = {**schema_defaults(SETTINGS_SCHEMA), **(ctx.settings or {})}
        symbols = clean_symbols(settings["symbols"])
        labels = parse_labels(settings["labels"])
        yrange = str(settings["range"])
        board = load(
            symbols,
            labels,
            yrange,
            now=ctx.now,
            cache_dir=ctx.cache_dir,
            online=ctx.online,
            http_timeout=ctx.http_timeout,
        )
        style = Style(ctx.fonts)
        c = Canvas(background=WHITE)
        if board.state == "empty":
            render_notice(
                c,
                style,
                ctx.now,
                headline="Which symbols?",
                line="Add a symbol in Apps > Stocks",
                footnote="One per line, as Yahoo Finance writes them: AAPL, ^GSPC, BTC-USD",
            )
        elif board.state == "unreachable":
            if ctx.online:
                line, foot = f"Can't reach {PROVIDER}", "Trying again at the next refresh"
            else:
                line, foot = "Offline, no quotes cached yet", "The quotes appear once back online"
            render_notice(
                c,
                style,
                ctx.now,
                headline="No quotes yet",
                line=line,
                footnote=f"{PROVIDER} {DOT} {foot}",
                line_fill=RED,
            )
        else:
            variant = pick_variant(ctx, settings)
            RENDERERS[variant](
                c, board, ctx.now, style, percent=bool(settings["show_percent"]), yrange=yrange
            )
        return c.to_image()


APP = StocksApp()
