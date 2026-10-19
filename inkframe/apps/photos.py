"""Photos app: STUB written in Phase 6 so the scheduler has a photos slot.

PHASE 3 REPLACES THIS WHOLE FILE with the real app (cached dithered renders
of the uploaded photos).  The contract the scheduler relies on, which the
real app must keep:

* ``APP.id == "photos"`` and ``settings_schema == ()``: per-slot behaviour
  (``dwell_minutes``, ``order``) lives in the rotation slot's ``options``,
  and the scheduler chooses which photo to show;
* ``render(ctx)`` returns the ``(400, 600)`` palette-index frame for the photo
  ``ctx.item_id`` (``ctx.db``/``ctx.paths``/``ctx.app_settings`` are set), or
  the "no photos" card when ``ctx.item_id`` is ``None``;
* an unknown photo id raises ``AppError`` (the scheduler logs it and skips).

Until then a photo renders as a placeholder card with its name and id.
"""

from __future__ import annotations

from PIL import Image

from inkframe.apps.base import AppError, Field, RenderContext
from inkframe.apps.draw import Canvas
from inkframe.display.palette import BLACK, BLUE, GREEN, RED, WHITE, YELLOW

#: keyline inset from the panel edge
_INSET = 22


class PhotosApp:
    """The photos slot (stub; see the module docstring)."""

    id = "photos"
    name = "Photos"
    description = "Your uploaded photos"
    needs_dither = False
    settings_schema: tuple[Field, ...] = ()
    variants = 1

    def render(self, ctx: RenderContext) -> Image.Image:
        c = Canvas(background=WHITE)
        c.rect(
            (_INSET, _INSET, c.width - 1 - _INSET, c.height - 1 - _INSET), outline=BLACK, width=2
        )
        if ctx.item_id is None:
            self._empty(c, ctx)
            return c.to_image()
        photo = ctx.db.get_photo(ctx.item_id) if ctx.db is not None else None
        if photo is None:
            raise AppError(f"photo {ctx.item_id} not found")
        mono = ctx.fonts.font("mono", 16)
        c.text((_INSET + 22, _INSET + 20), "PHOTO", mono, BLACK)
        c.text((c.width - _INSET - 22, _INSET + 20), photo.id[:8], mono, BLACK, anchor="ra")
        font, lines, _ = c.fit_text(
            photo.original_name,
            480,
            150,
            lambda size: ctx.fonts.font("serif", size),
            max_size=56,
            min_size=20,
        )
        lh = c.line_height(font, 1.15)
        y = 200 - len(lines) * lh // 2
        for line in lines[:3]:
            c.text((300, y), line, font, BLACK, anchor="ma")
            y += lh
        c.rect((270, y + 16, 329, y + 19), fill=RED)
        shape = f"{photo.width} × {photo.height}" + (" · portrait" if photo.portrait else "")
        c.text((300, c.height - _INSET - 26), shape, mono, BLACK, anchor="ms")
        return c.to_image()

    @staticmethod
    def _empty(c: Canvas, ctx: RenderContext) -> None:
        """The "no photos yet" card: serif headline, the upload address in mono,
        and the six panel colours as a small row of swatches."""
        serif = ctx.fonts.font("serif", 54)
        mono = ctx.fonts.font("mono", 18)
        c.text((300, 186), "No photos yet", serif, BLACK, anchor="ms")
        c.rect((270, 208, 329, 211), fill=RED)
        c.text((300, 256), "Upload at http://inkframe.local/", mono, BLACK, anchor="ms")
        x = 300 - (5 * 26 + 18) // 2
        for colour in (BLACK, RED, YELLOW, GREEN, BLUE):
            c.rect((x, 300, x + 17, 317), fill=colour)
            x += 26
        c.rect((x, 300, x + 17, 317), outline=BLACK, width=2)


APP = PhotosApp()
