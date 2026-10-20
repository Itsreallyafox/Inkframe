"""The photo pipeline: master -> fit/letterbox -> adjust -> dither (last, at 1:1).

Plan section 5.  Dithering is a per-pixel pattern tuned to the panel's grid,
so it is always the last step and always at the panel's exact 600 x 400:

1. **fit** (``fit_image``): scale the master to fit inside 600 x 400 keeping
   its aspect (Lanczos, up or down), centre it on white ``(255, 255, 255)``.
   Portrait photos are letterboxed too, never rotated.  The result is a
   ``Fit``: the 600 x 400 RGB canvas plus the box the photo occupies.
2. **adjust** (``inkframe.render.adjust``): brightness, contrast, saturation,
   gamma, sharpen, applied to the photo inside the box only.
3. **dither** (``inkframe.render.dither``): the adjusted photo against the
   recipe's palette; the bars outside the box are palette white (index 1)
   exactly.  Dithering only the box keeps the bars clean whatever the
   adjustments (brightness 0.5 would otherwise turn them into grey speckle)
   and stops error diffusing between the bars and the photo's edges.

A **recipe** is the complete dict ``inkframe.config.DEFAULT_RECIPE``
describes; ``complete_recipe`` validates one and also checks the method
against the dither registry.  ``recipe_key`` content-addresses a render: a
16-hex sha256 prefix over the canonical JSON of the recipe, the palette that
is dithered against, the palette the PNG is coloured with,
``FORMAT_VERSION`` and ``engine_version()`` (the native engine's ABI and a
digest of its source and blue-noise texture).  The master is not part of the
key because renders live in a per-photo directory and masters never change:
``masters.ingest`` refuses to overwrite one, and a deleted photo's renders
are deleted with it.

Fits are memoised in a small in-process LRU (``FIT_CACHE_SIZE`` entries,
keyed by photo id and the master's mtime and size) so a preview set, or a
preview followed by a save, decodes and resizes the master once.

Everything here is CPU-heavy; callers run it on the render executor.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from PIL import Image

from inkframe.config import DEFAULT_RECIPE, RECIPE_RANGES, validate_recipe
from inkframe.display.palette import NOMINAL, PANEL_HEIGHT, PANEL_WIDTH, WHITE, Palette
from inkframe.render import adjust
from inkframe.render import dither as dither_engine
from inkframe.render.dither import METHOD_IDS, METHODS_BY_ID

if TYPE_CHECKING:
    from inkframe.config import Settings
    from inkframe.db import Photo
    from inkframe.paths import DataPaths

log = logging.getLogger("inkframe.render.pipeline")

#: bump whenever the output for an unchanged recipe changes for a reason the
#: key cannot see: the fit, an adjust stage, the PNG layout, or a change on
#: the Python side of the dither engine (``dither.py``) that alters its
#: output.  Every render key changes with it.  Changes to ``dither.c`` and to
#: the blue-noise texture are covered by ``engine_version()``.
FORMAT_VERSION = 1
#: hex characters of a render key
KEY_LENGTH = 16
PANEL_SIZE = (PANEL_WIDTH, PANEL_HEIGHT)
#: letterbox colour of the fit stage
BAR_RGB = (255, 255, 255)
#: fitted masters kept in memory (720 KB each)
FIT_CACHE_SIZE = 4
#: distinct "ignoring a stored knob" warnings logged once each (a list of
#: photos resolves the same default recipe once per photo)
_WARN_ONCE_MAX = 256

Box = tuple[int, int, int, int]
FULL_BOX: Box = (0, 0, PANEL_WIDTH, PANEL_HEIGHT)


class RecipeError(ValueError):
    """A recipe failed validation; the message names the field."""


class MasterMissing(RuntimeError):
    """A photo's master file is missing or unreadable, so it cannot render."""


# --- recipes --------------------------------------------------------------------------


def complete_recipe(recipe: object, base: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate ``recipe`` (merged over ``base`` when given) into a complete,
    normalised recipe dict.  Raises ``RecipeError`` naming the bad field,
    including a method the dither registry does not know."""
    if recipe is not None and not isinstance(recipe, Mapping):
        raise RecipeError(f"recipe must be an object, got {type(recipe).__name__}")
    data: dict[str, Any] = dict(base or {})
    data.update(recipe or {})
    for knob, (lo, hi) in RECIPE_RANGES.items():
        value = data.get(knob)
        # a JSON integer too large for a float (1e400 spelt out) would make
        # validate_recipe's float() raise OverflowError: name the field instead
        if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > 1000:
            raise RecipeError(f"recipe.{knob} must be between {lo} and {hi}, got a huge number")
    try:
        out = validate_recipe(data)
    except (ValueError, OverflowError, TypeError) as exc:  # anything else odd is still a 422
        raise RecipeError(str(exc)) from None
    if out["method"] not in METHODS_BY_ID:
        raise RecipeError(
            f"recipe.method must be one of {', '.join(METHOD_IDS)}, got {out['method']!r}"
        )
    return out


_warned: set[str] = set()


def _warn_once(message: str) -> None:
    """Log ``message`` as a warning the first time it is seen."""
    if message in _warned:
        return
    if len(_warned) < _WARN_ONCE_MAX:
        _warned.add(message)
    log.warning("%s", message)


def _salvage(stored: object, base: Mapping[str, Any], owner: str) -> dict[str, Any]:
    """``stored`` merged over the complete ``base`` knob by knob: a knob that
    does not validate (a method dropped from the registry, a range that
    narrowed, a retired knob) keeps ``base``'s value, every other one
    applies."""
    merged = dict(base)
    items = stored.items() if isinstance(stored, Mapping) else ()
    for knob, value in items:
        try:
            merged = complete_recipe({knob: value}, base=merged)
        except RecipeError as exc:
            _warn_once(f"{owner}: ignoring its stored {knob} ({exc})")
    return merged


def default_recipe(settings: Settings) -> dict[str, Any]:
    """The settings' default recipe, complete.

    ``settings.json`` (``PUT /api/settings``) is validated without the dither
    registry, so it can hold a method this build does not have: such a knob
    falls back to the built-in default's value alone, and the default's
    other knobs still apply (as for a photo's override)."""
    try:
        return complete_recipe(settings.default_recipe)
    except RecipeError:
        return _salvage(settings.default_recipe, complete_recipe(DEFAULT_RECIPE), "default recipe")


def effective_recipe(photo: Photo, settings: Settings) -> dict[str, Any]:
    """The recipe a photo renders with: its override (merged over the default,
    so a recipe stored by an older version gains new knobs) or the default.

    A stored value that no longer validates (a method dropped from the
    registry, a range that narrowed) is replaced by the default's value for
    that knob alone; the override's other knobs still apply."""
    base = default_recipe(settings)
    if photo.recipe is None:
        return base
    try:
        return complete_recipe(photo.recipe, base=base)
    except RecipeError:
        return _salvage(photo.recipe, base, f"photo {photo.id}")


def resolve_render_palette(recipe: Mapping[str, Any], settings: Settings) -> Palette:
    """The palette a recipe dithers against: the measured panel colours from
    settings for ``"measured"``, the nominal primaries otherwise."""
    return settings.measured_palette() if recipe["palette"] == "measured" else NOMINAL


def recipe_key(
    recipe: Mapping[str, Any], render_palette: Palette, measured_palette: Palette
) -> str:
    """The render cache key: 16 hex characters of sha256 over the canonical
    JSON of the complete recipe, the palette dithered against, the palette
    the PNG is coloured with, ``FORMAT_VERSION`` and ``engine_version()``.

    Equal inputs give equal keys whatever the dict order; any knob or palette
    change gives a new key.  Raises ``RecipeError`` for an invalid recipe.
    """
    payload = {
        "format": FORMAT_VERSION,
        "engine": engine_version(),
        "recipe": complete_recipe(recipe),
        "dither_palette": [list(c) for c in render_palette.rgb],
        "png_palette": [list(c) for c in measured_palette.rgb],
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:KEY_LENGTH]


@functools.lru_cache(maxsize=1)
def engine_version() -> str:
    """The dither engine's output version: its ABI plus a digest of
    ``dither.c`` (kernels, Lab tables) and the blue-noise texture, so a change
    to either changes every render key even when the ABI stays the same.
    The Pi rebuilds the library whenever ``dither.c`` changes (install.sh)."""
    digest = hashlib.sha256()
    for path in (Path(dither_engine.__file__).with_name("dither.c"), dither_engine.BLUE_NOISE_PATH):
        try:
            digest.update(path.read_bytes())
        except OSError:
            log.warning("cannot read %s for the engine version", path)
            digest.update(b"missing:" + path.name.encode())
    return f"{dither_engine.ABI_VERSION}:{digest.hexdigest()[:16]}"


def key_for(recipe: Mapping[str, Any], settings: Settings) -> str:
    """``recipe_key`` with the palettes ``settings`` resolve for ``recipe``."""
    return recipe_key(recipe, resolve_render_palette(recipe, settings), settings.measured_palette())


# --- fit -------------------------------------------------------------------------------


def fit_geometry(width: int, height: int) -> Box:
    """The box ``(x0, y0, x1, y1)`` a ``width`` x ``height`` image occupies
    when scaled to fit inside 600 x 400 and centred.  Exact 3:2 fills it."""
    if width < 1 or height < 1:
        raise ValueError(f"image has no pixels ({width} x {height})")
    s = min(PANEL_WIDTH / width, PANEL_HEIGHT / height)
    w = min(PANEL_WIDTH, max(1, round(width * s)))
    h = min(PANEL_HEIGHT, max(1, round(height * s)))
    x0 = (PANEL_WIDTH - w) // 2
    y0 = (PANEL_HEIGHT - h) // 2
    return (x0, y0, x0 + w, y0 + h)


@dataclass(frozen=True)
class Fit:
    """A master fitted to the panel.  ``image`` is shared through the LRU:
    treat it as read-only (every pipeline step returns new images)."""

    #: 600 x 400 RGB, white bars around the photo
    image: Image.Image
    #: where the photo sits inside ``image``
    box: Box


def fit_image(master: Image.Image) -> Fit:
    """Scale ``master`` to fit inside 600 x 400 (Lanczos, keeping aspect) and
    centre it on a white canvas."""
    rgb = master if master.mode == "RGB" else master.convert("RGB")
    box = fit_geometry(*rgb.size)
    size = (box[2] - box[0], box[3] - box[1])
    content = rgb if rgb.size == size else rgb.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", PANEL_SIZE, BAR_RGB)
    canvas.paste(content, box[:2])
    return Fit(canvas, box)


def fit_master(master_image: Image.Image) -> Image.Image:
    """``fit_image`` without the box: the 600 x 400 letterboxed RGB image."""
    return fit_image(master_image).image


# --- adjust + dither ----------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    """A fit after the adjustments: the box's pixels, ready to dither."""

    rgb: np.ndarray
    box: Box


def as_fit(fit_rgb: Fit | Image.Image, box: Box | None = None) -> Fit:
    """A ``Fit`` from a ``Fit`` (``box`` overrides its box) or from a 600 x 400
    image (whose box is ``box``, default the whole image)."""
    if isinstance(fit_rgb, Fit):
        return fit_rgb if box is None else Fit(fit_rgb.image, box)
    if not isinstance(fit_rgb, Image.Image):
        raise TypeError(f"expected a Fit or a PIL image, got {type(fit_rgb).__name__}")
    if fit_rgb.size != PANEL_SIZE:
        raise ValueError(
            f"fit must be {PANEL_WIDTH}x{PANEL_HEIGHT}, got {fit_rgb.size[0]}x{fit_rgb.size[1]}"
        )
    return Fit(fit_rgb if fit_rgb.mode == "RGB" else fit_rgb.convert("RGB"), box or FULL_BOX)


def prepare(fit: Fit, recipe: Mapping[str, Any]) -> Prepared:
    """Apply a recipe's adjustments to the photo inside ``fit.box``."""
    content = fit.image if fit.box == FULL_BOX else fit.image.crop(fit.box)
    adjusted = adjust.apply_recipe(content, recipe)
    return Prepared(np.asarray(adjusted, dtype=np.uint8), fit.box)


def dither_prepared(
    prepared: Prepared, recipe: Mapping[str, Any], render_palette: Palette
) -> np.ndarray:
    """Dither prepared pixels with a recipe's method and knobs; returns the
    full ``(400, 600)`` uint8 index frame (palette white outside the box)."""
    idx = dither_engine.dither(
        prepared.rgb,
        render_palette,
        recipe["method"],
        serpentine=bool(recipe["serpentine"]),
        strength=float(recipe["strength"]),
        distance=recipe["distance"],
    )
    if prepared.box == FULL_BOX:
        return idx
    x0, y0, x1, y1 = prepared.box
    frame = np.full((PANEL_HEIGHT, PANEL_WIDTH), WHITE, dtype=np.uint8)
    frame[y0:y1, x0:x1] = idx
    return frame


def render_frame(
    fit_rgb: Fit | Image.Image,
    recipe: Mapping[str, Any],
    render_palette: Palette,
    *,
    box: Box | None = None,
) -> np.ndarray:
    """Adjust then dither a fitted photo: the ``(400, 600)`` uint8 index frame.

    ``fit_rgb`` is a ``Fit`` (only its box is adjusted and dithered) or a
    600 x 400 RGB image (the whole image, or ``box`` when given).  ``recipe``
    is validated first (``RecipeError``).
    """
    r = complete_recipe(recipe)
    return dither_prepared(prepare(as_fit(fit_rgb, box), r), r, render_palette)


# --- masters on disk + the fit LRU ----------------------------------------------------------


class _FitLRU:
    """A tiny thread-safe LRU of ``Fit`` objects."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._items: OrderedDict[tuple[Any, ...], Fit] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[Any, ...]) -> Fit | None:
        with self._lock:
            fit = self._items.get(key)
            if fit is not None:
                self._items.move_to_end(key)
            return fit

    def put(self, key: tuple[Any, ...], fit: Fit) -> None:
        with self._lock:
            self._items[key] = fit
            self._items.move_to_end(key)
            while len(self._items) > self.size:
                self._items.popitem(last=False)

    def forget(self, photo_id: str) -> None:
        with self._lock:
            for key in [k for k in self._items if k[0] == photo_id]:
                del self._items[key]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


_FITS = _FitLRU(FIT_CACHE_SIZE)


def master_file(photo: Photo, paths: DataPaths) -> Path:
    """Absolute path of a photo's master (``Photo.master_path`` is relative
    to the data directory).  Raises ``MasterMissing`` for a path that does not
    stay inside ``<data>/masters`` (absolute elsewhere, ``..``), so nothing
    outside it is ever read or deleted because of a database value."""
    path = paths.root / Path(photo.master_path)
    try:
        path.resolve().relative_to(paths.masters.resolve())
    except (ValueError, OSError):
        raise MasterMissing(
            f"photo {photo.id} has a master path outside the masters directory: "
            f"{photo.master_path!r}"
        ) from None
    return path


def load_fit(photo: Photo, paths: DataPaths) -> Fit:
    """The photo's master, fitted; memoised by (photo id, path, mtime, size).

    Raises ``MasterMissing`` if the master file is gone or unreadable.
    """
    path = master_file(photo, paths)
    try:
        st = path.stat()
    except FileNotFoundError:
        raise MasterMissing(f"the master of photo {photo.id} is missing ({path})") from None
    key = (photo.id, str(path), st.st_mtime_ns, st.st_size)
    fit = _FITS.get(key)
    if fit is not None:
        return fit
    try:
        im = Image.open(path)
        im.load()  # opened by name: Pillow closes the file once loaded
    except FileNotFoundError:
        raise MasterMissing(f"the master of photo {photo.id} is missing ({path})") from None
    except Exception as exc:  # corrupt JPEG, permissions...
        raise MasterMissing(f"the master of photo {photo.id} is unreadable: {exc}") from None
    fit = fit_image(im)
    _FITS.put(key, fit)
    return fit


def forget_fit(photo_id: str) -> None:
    """Drop a photo's fits from the LRU (after a delete)."""
    _FITS.forget(photo_id)


def clear_fit_cache() -> None:
    """Empty the LRU (tests)."""
    _FITS.clear()
