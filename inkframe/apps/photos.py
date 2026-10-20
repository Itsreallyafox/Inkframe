"""Photos: the panel shows one uploaded photo, dithered with its recipe.

The scheduler picks the photo (shuffle or sequential, dwell time: plan
section 9) and passes its id as ``RenderContext.item_id``; this app only
turns that id into a frame.  The frame comes from the render cache
(``inkframe.render.cache``), rendering on a miss, so what the panel shows is
byte for byte the PNG the Photos page displays for the same recipe.

``needs_dither`` is False because the app returns a finished palette-index
frame: the photo was already dithered by the pipeline with its own recipe,
and the "no photos" card is drawn in exact palette colours.

* ``item_id`` is ``None``, or the photo was deleted: a tidy "No photos yet"
  card (the Apps page preview shows it too).
* the photo exists but its master file is missing or unreadable:
  ``AppError``, so the scheduler logs it and moves on.
* ``ctx.app_settings`` must be the global ``Settings`` snapshot (it picks
  the recipe and palette); anything else is an ``AppError``, not a quiet
  render with default settings.
"""

from __future__ import annotations

import logging

import numpy as np

from inkframe.apps.base import AppError, Field, FontLibrary, RenderContext
from inkframe.apps.draw import Canvas
from inkframe.config import Settings
from inkframe.display.palette import BLACK, BLUE, GREEN, RED, WHITE, YELLOW
from inkframe.render.cache import RenderCache
from inkframe.render.dither import DitherError
from inkframe.render.pipeline import MasterMissing

log = logging.getLogger("inkframe.apps.photos")

UPLOAD_HINT = "Upload at http://inkframe.local/"
HEADLINE = "No photos yet"
SUBLINE = "Add some from your phone or computer."

W, H = 600, 400


def _camera(c: Canvas, cx: int, top: int) -> None:
    """A small camera drawn from primitives: black body, blue lens, yellow
    flash, red shutter button.  ``top`` is the top of the viewfinder hump;
    the glyph is 140 x 106 pixels."""
    c.rect((cx - 30, top, cx + 30, top + 26), fill=BLACK, radius=6)  # viewfinder hump
    c.rect((cx - 60, top + 6, cx - 40, top + 18), fill=RED, radius=3)  # shutter button
    c.rect((cx - 70, top + 16, cx + 70, top + 106), fill=BLACK, radius=14)  # body
    c.ellipse((cx - 36, top + 25, cx + 36, top + 97), fill=WHITE)  # lens rim
    c.ellipse((cx - 28, top + 33, cx + 28, top + 89), fill=BLUE)  # glass
    c.ellipse((cx - 13, top + 48, cx + 13, top + 74), fill=BLACK)  # aperture
    c.ellipse((cx + 3, top + 51, cx + 11, top + 59), fill=WHITE)  # glint
    c.rect((cx + 44, top + 28, cx + 60, top + 38), fill=YELLOW, radius=2)  # flash


def no_photos_card(fonts: FontLibrary) -> Canvas:
    """The card shown when there is no photo to show."""
    c = Canvas(WHITE)
    cx = W // 2
    _camera(c, cx, 52)

    headline = fonts.font("serif", 54, "bold")
    c.text((cx, 230), HEADLINE, headline, BLACK, anchor="ms")
    sub = fonts.font("sans", 20)
    c.text((cx, 266), SUBLINE, sub, BLACK, anchor="ms")

    # the address in a pill, like a browser's address bar
    mono = fonts.font("mono", 19)
    half = c.text_width(UPLOAD_HINT, mono) // 2 + 16
    c.rect((cx - half, 298, cx + half, 336), outline=BLACK, width=2, radius=19)
    c.text((cx, 317), UPLOAD_HINT, mono, BLUE, anchor="mm")

    # the panel's own colours as a signature strip along the bottom edge
    bands = (YELLOW, RED, BLUE, GREEN, BLACK)
    band_w = W // len(bands)
    for i, colour in enumerate(bands):
        x0 = i * band_w
        x1 = W if i == len(bands) - 1 else x0 + band_w
        c.rect((x0, H - 10, x1 - 1, H - 1), fill=colour)
    return c


class PhotosApp:
    """``APP`` of ``inkframe.apps.photos``."""

    id = "photos"
    name = "Photos"
    description = "Your uploaded photos"
    needs_dither = False
    settings_schema: tuple[Field, ...] = ()
    variants = 1

    def render(self, ctx: RenderContext) -> np.ndarray:
        """The ``(400, 600)`` index frame for ``ctx.item_id`` (rendered and
        cached on a miss), or the "no photos" card."""
        if ctx.item_id is None or ctx.db is None:
            return no_photos_card(ctx.fonts).to_frame()
        photo = ctx.db.get_photo(ctx.item_id)
        if photo is None:
            log.info("photo %s is gone; showing the no-photos card", ctx.item_id)
            return no_photos_card(ctx.fonts).to_frame()
        if ctx.paths is None:
            raise AppError("the photos app needs the data paths to find renders")
        settings = ctx.app_settings
        if not isinstance(settings, Settings):
            # never guess: default settings would dither with the default
            # palette and recipe, under keys the Photos page never shows
            raise AppError(
                "the photos app needs the global Settings snapshot as ctx.app_settings, "
                f"got {type(settings).__name__}"
            )
        try:
            return RenderCache(ctx.paths).render_or_get(photo, None, settings).frame
        except MasterMissing as exc:
            raise AppError(str(exc)) from None
        except DitherError as exc:
            raise AppError(f"cannot dither photo {photo.id}: {exc}") from None


APP = PhotosApp()
