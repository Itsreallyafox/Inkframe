"""The render cache: dithered frames on disk, content-addressed by recipe key.

Layout::

    <data>/renders/<photo_id>/<key>.png

``key`` is ``pipeline.recipe_key(...)`` (16 hex characters), so a file name
fully describes its contents and a URL carrying the key can be cached by the
browser forever.  Each PNG is a 4-bit paletted (``"P"``) image of 600 x 400:

* **the pixel values are the frame**: palette indices 0..5 (black, white,
  yellow, red, blue, green), exactly what the panel is sent;
* the PNG's six-entry colour table is the *measured* palette, so a browser
  shows what the panel shows.  That mapping is informational only: reading a
  render back (``get``) uses the indices and ignores the colours.

Invalidation: a recipe or palette change produces a new key, so stale files
are simply never asked for again.  ``prune`` keeps at most
``MAX_RENDERS_PER_PHOTO`` files per photo (oldest written go first, the
photo's current render never), and ``invalidate`` drops a photo's directory
when the photo is deleted.  Writes are atomic (temp file + rename), every
read tolerates a missing file or directory, and a corrupt file is treated as
a miss and removed.

All writes happen on the render executor; reads may come from any thread.
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from PIL import Image

from inkframe.display.base import validate_frame
from inkframe.display.palette import N_COLORS, PANEL_HEIGHT, PANEL_WIDTH, Palette
from inkframe.paths import DataPaths, atomic_write_bytes
from inkframe.render import pipeline
from inkframe.render.dither import METHODS, DitherMethod
from inkframe.render.masters import valid_photo_id

if TYPE_CHECKING:
    from inkframe.config import Settings
    from inkframe.db import Photo

log = logging.getLogger("inkframe.render.cache")

#: files kept per photo by ``prune`` (one preview set is 12)
MAX_RENDERS_PER_PHOTO = 40
#: an abandoned atomic-write temp file older than this is swept by ``prune``
STALE_TMP_SECONDS = 600
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_KEY = re.compile(r"[0-9a-f]{16}")

#: what ``render_or_get`` asks for when the master is not yet fitted
MasterLoader = Callable[["Photo"], "pipeline.Fit | Image.Image"]


def valid_key(key: object) -> bool:
    """A render key: 16 lowercase hex characters."""
    return isinstance(key, str) and _KEY.fullmatch(key) is not None


def encode_png(indices: np.ndarray, palette: Palette) -> bytes:
    """A ``(400, 600)`` index frame as a paletted PNG coloured with ``palette``."""
    frame = validate_frame(indices)
    im = Image.fromarray(frame)  # mode "L"; putpalette turns it into "P"
    im.putpalette(palette.flat())
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def decode_png(data: bytes) -> np.ndarray | None:
    """The index frame stored in a render PNG, or None if ``data`` is not one
    (wrong mode, shape or an index outside 0..5)."""
    if not data.startswith(PNG_MAGIC):
        return None
    try:
        with Image.open(io.BytesIO(data), formats=["PNG"]) as im:
            if im.mode != "P":
                return None
            frame = np.array(im, dtype=np.uint8)
    except Exception:  # truncated / corrupt
        return None
    if frame.shape != (PANEL_HEIGHT, PANEL_WIDTH) or (frame.size and frame.max() >= N_COLORS):
        return None
    return frame


@dataclass(frozen=True)
class Rendered:
    """One frame from ``render_or_get``."""

    key: str
    frame: np.ndarray
    #: True if it came from disk, False if it was rendered now
    cached: bool
    #: the complete recipe it was rendered with
    recipe: dict[str, Any]


@dataclass(frozen=True)
class MethodRender:
    """One entry of a preview set (``render_methods``)."""

    method: DitherMethod
    key: str
    cached: bool


class RenderCache:
    """Read and write ``<data>/renders``.  Cheap to construct; holds no state
    besides the paths, so every caller can make its own."""

    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths

    # --- paths ---------------------------------------------------------------------
    def dir(self, photo_id: str) -> Path:
        """``<data>/renders/<photo_id>``; ``ValueError`` for a malformed id."""
        if not valid_photo_id(photo_id):
            raise ValueError(f"not a photo id: {photo_id!r}")
        return self.paths.renders / photo_id

    def path(self, photo_id: str, key: str) -> Path:
        """``<data>/renders/<photo_id>/<key>.png``; ``ValueError`` for a malformed key."""
        if not valid_key(key):
            raise ValueError(f"not a render key: {key!r}")
        return self.dir(photo_id) / f"{key}.png"

    # --- reads -----------------------------------------------------------------------
    def has(self, photo_id: str, key: str) -> bool:
        """True if the render exists on disk (not decoded)."""
        return self.path(photo_id, key).is_file()

    def read_png(self, photo_id: str, key: str) -> bytes | None:
        """The PNG bytes of a render, or None on a miss.  The file is
        validated as ``get`` does (a 600 x 400 frame of indices 0..5), so a
        corrupt or foreign file is never served (it is removed instead)."""
        path = self.path(photo_id, key)
        try:
            data = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None
        if decode_png(data) is None:
            log.warning("discarding corrupt render %s", path)
            self._unlink(path)
            return None
        return data

    def get(self, photo_id: str, key: str) -> np.ndarray | None:
        """The cached ``(400, 600)`` index frame, or None on a miss.  A file
        that does not decode to a valid frame is removed and reported as a miss."""
        path = self.path(photo_id, key)
        try:
            data = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None
        frame = decode_png(data)
        if frame is None:
            log.warning("discarding corrupt render %s", path)
            self._unlink(path)
        return frame

    def keys(self, photo_id: str) -> list[str]:
        """Keys of the renders on disk for a photo (any order)."""
        try:
            return [p.stem for p in self.dir(photo_id).glob("*.png") if valid_key(p.stem)]
        except FileNotFoundError:
            return []

    # --- writes ---------------------------------------------------------------------
    def put(self, photo_id: str, key: str, indices: np.ndarray, measured_palette: Palette) -> Path:
        """Store a frame atomically, coloured with ``measured_palette``."""
        path = self.path(photo_id, key)
        data = encode_png(indices, measured_palette)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, data)
        return path

    def invalidate(self, photo_id: str) -> None:
        """Delete every render of a photo (its directory).  Missing is fine."""
        d = self.dir(photo_id)
        try:
            shutil.rmtree(d)
        except FileNotFoundError:
            pass
        except OSError:
            # e.g. Windows refusing to delete a file another process has open
            log.warning("could not remove all renders in %s", d, exc_info=True)

    def prune(
        self,
        photo_id: str,
        keep_keys: Iterable[str] = (),
        max_files: int = MAX_RENDERS_PER_PHOTO,
    ) -> int:
        """Delete the oldest renders of a photo beyond ``max_files``, never one
        in ``keep_keys``.  Also sweeps abandoned temp files.  Returns how many
        renders were removed."""
        d = self.dir(photo_id)
        keep = set(keep_keys)
        entries: list[tuple[int, str, Path]] = []
        now = time.time()
        try:
            listing = list(d.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return 0
        for p in listing:
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            if p.suffix == ".tmp" and p.name.startswith("."):
                if now - st.st_mtime > STALE_TMP_SECONDS:
                    self._unlink(p)
                continue
            if p.suffix == ".png":
                entries.append((st.st_mtime_ns, p.name, p))
        excess = len(entries) - max_files
        if excess <= 0:
            return 0
        entries.sort()
        removed = 0
        for _mtime, _name, p in entries:
            if removed >= excess:
                break
            if p.stem in keep:
                continue
            if self._unlink(p):
                removed += 1
        return removed

    @staticmethod
    def _unlink(path: Path) -> bool:
        try:
            path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            log.warning("could not remove %s", path, exc_info=True)
            return False
        return True

    # --- rendering ---------------------------------------------------------------------
    def _loader(self, master_loader: MasterLoader | None) -> MasterLoader:
        if master_loader is not None:
            return master_loader
        return lambda photo: pipeline.load_fit(photo, self.paths)

    def render_or_get(
        self,
        photo: Photo,
        recipe: Any,
        settings: Settings,
        master_loader: MasterLoader | None = None,
    ) -> Rendered:
        """The frame for ``photo`` under ``recipe`` (``None``: its effective
        recipe): from disk, or rendered now and stored.

        ``master_loader(photo)`` returns the fitted master (default: the
        pipeline's LRU-backed ``load_fit``).  Raises ``RecipeError``,
        ``MasterMissing`` or the dither engine's errors.  CPU-heavy on a miss:
        call it from the render executor.
        """
        if recipe is None:
            r = pipeline.effective_recipe(photo, settings)
        else:
            r = pipeline.complete_recipe(recipe)
        render_palette = pipeline.resolve_render_palette(r, settings)
        measured = settings.measured_palette()
        key = pipeline.recipe_key(r, render_palette, measured)
        hit = self.get(photo.id, key)
        if hit is not None:
            return Rendered(key, hit, True, r)
        fit = self._loader(master_loader)(photo)
        frame = pipeline.render_frame(fit, r, render_palette)
        self.put(photo.id, key, frame, measured)
        return Rendered(key, frame, False, r)

    def render_methods(
        self,
        photo: Photo,
        recipe: Any,
        settings: Settings,
        methods: Sequence[DitherMethod] = METHODS,
        master_loader: MasterLoader | None = None,
    ) -> list[MethodRender]:
        """Ensure a render exists for every method with ``recipe``'s other
        knobs (the editor's method strip).  The master is fitted and adjusted
        at most once for the whole set.  A cached method costs one read and
        decode of its PNG: a corrupt or foreign file under its key is removed
        and rendered again, since a preview key is not the photo's current
        one and its URL could not re-render it later."""
        base = pipeline.complete_recipe(recipe)
        render_palette = pipeline.resolve_render_palette(base, settings)
        measured = settings.measured_palette()
        prepared: pipeline.Prepared | None = None
        out: list[MethodRender] = []
        for method in methods:
            r = {**base, "method": method.id}
            key = pipeline.recipe_key(r, render_palette, measured)
            cached = self.get(photo.id, key) is not None
            if not cached:
                if prepared is None:
                    fit = self._loader(master_loader)(photo)
                    prepared = pipeline.prepare(pipeline.as_fit(fit, None), base)
                frame = pipeline.dither_prepared(prepared, r, render_palette)
                self.put(photo.id, key, frame, measured)
            out.append(MethodRender(method, key, cached))
        return out
