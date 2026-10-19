"""Patterns: generative six-colour compositions, and the palette test card.

Every card is composed directly in the six palette colours (``needs_dither =
False``) and is meant to look like a screen print: few flat colours, strong
figure and ground, either a deliberate full bleed or a print on a white mount
with a 24 px margin, sometimes a keyline, and a small mono plate caption.

Families (``settings["family"]``), each a ``draw(rng, canvas, settings) ->
Plate`` function in its own module:

* ``truchet``    (``patterns_truchet``)    tiles: Smith arcs, filled arcs, double arcs,
  ten-print diagonals, half-square quilts; paths coloured as continuous lines
* ``bauhaus``    (``patterns_bauhaus``)    5-12 large primitives on a 6 x 4 grid,
  asymmetric, with textured fills, knock-outs and overprints
* ``halftone``   (``patterns_halftone``)   a dot (or line) screen whose dot size
  follows a large smooth shape: a lit sphere, glows, waves, ripples, noise
* ``flow``       (``patterns_flow``)       300-800 polylines traced through a
  seeded noise / curl / swirl field, with a quiet margin
* ``concentric`` (``patterns_concentric``) Albers squares, banded rings with
  inversions, two-centre interference, quarter-arc supergraphics, squares
* ``random``     one of the five, picked from the seed
* ``test_card``  (``patterns_testcard``)   the palette test card for hardware
  bring-up; deterministic, ignores the seed

Variants (``RenderContext.variant``; tests and previews force one, the
scheduler leaves it ``None`` and the ``family`` setting decides):

* ``0``: the configured family, first draw (``random`` picks a family from the seed)
* ``1``: a second draw: a different random family, or the same configured
  family with a different composition
* ``2``: the palette test card

Seed: ``ctx.seed`` when set, else the render time in whole seconds, so each
render is new while tests stay deterministic.  All randomness comes from
``numpy.random.default_rng`` seeded with ``[seed, draw, family]``.

Settings: ``family`` (select, default ``random``), ``density`` (1..5, default
3: fewer/larger to more/smaller elements), ``palette_mood`` (``full`` |
``duotone`` | ``warm`` | ``cool``, default ``full``: which colours a
composition may use; duotone is black plus one accent on white) and
``caption`` (bool, default on: the plate caption under mounted prints).

No network: this app never goes offline.  If a family fails (a bug), the
render logs it and falls back to the next family; ``AppError`` only if none
can draw.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
from PIL import Image

from inkframe.apps import (
    patterns_bauhaus,
    patterns_concentric,
    patterns_flow,
    patterns_halftone,
    patterns_testcard,
    patterns_truchet,
)
from inkframe.apps.base import AppError, Field, FontLibrary, RenderContext, coerce_field
from inkframe.apps.draw import Canvas
from inkframe.apps.patterns_common import H, Plate
from inkframe.display.palette import BLACK, WHITE

log = logging.getLogger("inkframe.apps.patterns")

#: the generative families, in the order ``random`` permutes them
FAMILIES: tuple[str, ...] = ("truchet", "bauhaus", "halftone", "flow", "concentric")

DrawFn = Callable[[np.random.Generator, Canvas, Mapping[str, Any]], Plate]
DRAW: dict[str, DrawFn] = {
    "truchet": patterns_truchet.draw,
    "bauhaus": patterns_bauhaus.draw,
    "halftone": patterns_halftone.draw,
    "flow": patterns_flow.draw,
    "concentric": patterns_concentric.draw,
}

SETTINGS_SCHEMA: tuple[Field, ...] = (
    Field(
        "family",
        "Pattern family",
        "select",
        "random",
        options=(
            ("random", "Random: a different family each time"),
            ("truchet", "Truchet tiles"),
            ("bauhaus", "Bauhaus composition"),
            ("halftone", "Halftone field"),
            ("flow", "Flow field"),
            ("concentric", "Concentric forms"),
            ("test_card", "Palette test card"),
        ),
        help="The test card shows the six panel colours for tuning the measured palette.",
    ),
    Field(
        "density",
        "Density",
        "number",
        3,
        min=1,
        max=5,
        step=1,
        help="1: a few large elements. 5: many small ones.",
    ),
    Field(
        "palette_mood",
        "Colours",
        "select",
        "full",
        options=(
            ("full", "All six colours"),
            ("duotone", "Duotone: black and one accent on white"),
            ("warm", "Warm: black, yellow and red"),
            ("cool", "Cool: black, blue and green"),
        ),
    ),
    Field(
        "caption",
        "Plate caption",
        "bool",
        True,
        help="A small caption (family and plate number) under mounted prints.",
    ),
)

_FIELDS = {f.key: f for f in SETTINGS_SCHEMA}


def resolved_settings(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Complete settings: known keys coerced, anything missing or invalid set
    to its default (a bad value never stops the frame)."""
    out: dict[str, Any] = {}
    raw = raw or {}
    for key, f in _FIELDS.items():
        if key in raw:
            try:
                out[key] = coerce_field(f, raw[key])
                continue
            except ValueError as e:
                log.warning("patterns: ignoring setting %s: %s", key, e)
        out[key] = coerce_field(f, f.default, check_required=False)
    return out


def resolve_seed(ctx: RenderContext) -> int:
    """``ctx.seed`` if set, else the render time in whole seconds."""
    seed = ctx.seed if ctx.seed is not None else int(ctx.now.timestamp())
    return int(seed) % (2**63)


def pick_family(seed: int, draw: int = 0) -> str:
    """The family ``random`` shows for ``seed``; draws 0 and 1 always differ."""
    order = np.random.default_rng([seed, 0x5EED]).permutation(len(FAMILIES))
    return FAMILIES[int(order[draw % len(FAMILIES)])]


def finish(canvas: Canvas, plate: Plate, fonts: FontLibrary, seed: int) -> None:
    """Keyline and plate caption around the family's final box."""
    x0, y0, x1, y1 = plate.box
    if plate.frame and not plate.bleed:
        g = 6
        canvas.rect((x0 - g, y0 - g, x1 - 1 + g, y1 - 1 + g), outline=BLACK, width=2)
    if not plate.caption or plate.bleed:
        return
    try:
        bold = fonts.font("mono", 14, "bold")
        regular = fonts.font("mono", 14, "regular")
    except Exception:  # a missing font must not cost the whole card
        log.warning("patterns: caption fonts unavailable", exc_info=True)
        return
    base = max(plate.caption_y, y1 + 26 if plate.frame else y1 + 22)
    base = min(H - 14, base)
    title = plate.title.upper()
    note = plate.note.upper()
    number = f"No. {seed % 10000:04d}"
    room = (x1 - x0) - canvas.text_width(number, regular) - 16
    if canvas.text_width(title, bold) + 10 + canvas.text_width(note, regular) > room:
        note = ""  # never let the caption run into the plate number
    box = canvas.text((x0, base), title, bold, BLACK, anchor="ls")
    if note:
        canvas.text((box[2] + 10, base), note, regular, BLACK, anchor="ls")
    canvas.text((x1, base), number, regular, BLACK, anchor="rs")


def compose(
    family: str,
    seed: int,
    draw: int,
    settings: Mapping[str, Any],
    fonts: FontLibrary,
) -> tuple[Canvas, Plate]:
    """Render one generative composition; returns the canvas and its plate."""
    rng = np.random.default_rng([seed, draw, FAMILIES.index(family)])
    canvas = Canvas(background=WHITE)
    plate = DRAW[family](rng, canvas, settings)
    finish(canvas, plate, fonts, seed)
    return canvas, plate


class PatternsApp:
    id = "patterns"
    name = "Patterns"
    description = "Generative six-colour prints: Truchet, Bauhaus, halftone, flow, concentric."
    needs_dither = False
    settings_schema: tuple[Field, ...] = SETTINGS_SCHEMA
    variants = 3

    def render(self, ctx: RenderContext) -> Image.Image:
        settings = resolved_settings(ctx.settings)
        variant = None if ctx.variant is None else int(ctx.variant) % self.variants
        if variant == 2 or settings["family"] == "test_card":
            return patterns_testcard.draw(ctx.fonts, ctx.measured_palette).to_image()
        seed = resolve_seed(ctx)
        draw = 1 if variant == 1 else 0
        family = settings["family"]
        if family not in FAMILIES:
            family = pick_family(seed, draw)
        start = FAMILIES.index(family)
        for k in range(len(FAMILIES)):
            fam = FAMILIES[(start + k) % len(FAMILIES)]
            try:
                canvas, _ = compose(fam, seed, draw, settings, ctx.fonts)
                return canvas.to_image()
            except Exception:  # a generative bug must never blank the frame
                log.exception("patterns: family %s failed for seed %s", fam, seed)
        raise AppError("no pattern family could be drawn")


APP = PatternsApp()
