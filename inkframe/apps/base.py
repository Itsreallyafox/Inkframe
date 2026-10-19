"""The app plugin contract (plan section 8) and the helpers every app shares.

An app renders one 600 x 400 card for the panel.  Two kinds exist:

* ``needs_dither = False`` (weather, headlines, patterns, quotes): the app
  composes *directly in the six palette colours* using ``inkframe.apps.draw``
  (flat fills, solid type, no anti-aliasing, no gradients) and returns an RGB
  ``PIL.Image`` whose pixels are exactly ``NOMINAL`` palette colours, or a
  ``(400, 600)`` palette-index array.  Dithered text looks terrible on e-ink;
  flat six-colour layouts look like printed cards.
* ``needs_dither = True`` (photos, or an illustrative element that needs
  tone): the app returns any RGB image and ``render_to_frame`` dithers it
  with a recipe through the Phase 2 engine.

``render_to_frame`` is the only entry point the scheduler and the preview
endpoint use; it turns whatever ``App.render`` returned into a validated
palette-index frame.

Settings: each app declares a ``settings_schema`` of ``Field`` objects.  The
Apps page renders a form from it, ``validate_settings`` coerces and checks
what comes back, and the app receives the complete, validated dict in
``RenderContext.settings``.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np
from PIL import Image, ImageFont

from inkframe.display.base import validate_frame
from inkframe.display.palette import (
    MEASURED_DEFAULT,
    NOMINAL,
    PANEL_HEIGHT,
    PANEL_WIDTH,
    Palette,
    rgb_to_indices_exact,
)

log = logging.getLogger("inkframe.apps")

FieldType = Literal["text", "textarea", "number", "bool", "select", "list"]
_FIELD_TYPES: tuple[str, ...] = ("text", "textarea", "number", "bool", "select", "list")


class AppError(RuntimeError):
    """An app could not render (network down with no cache, bad settings...).

    The scheduler logs it and skips the app; it never leaves the panel blank.
    """


# --- settings schema -------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One entry of an app's settings form.

    ``type`` decides the widget and the validation:

    * ``text`` / ``textarea``: ``str`` (``textarea`` is multi-line)
    * ``number``: ``int`` or ``float`` within ``min``..``max`` (``step`` is a UI hint;
      ``step == 1`` means the value is coerced to ``int``)
    * ``bool``: ``True``/``False``
    * ``select``: one of ``options`` (``(value, label)`` pairs)
    * ``list``: a list of non-empty strings (one per line in the UI), e.g. feed URLs
    """

    key: str
    label: str
    type: FieldType
    default: Any
    help: str = ""
    options: tuple[tuple[str, str], ...] = ()
    min: float | None = None
    max: float | None = None
    step: float | None = None
    placeholder: str = ""
    required: bool = False

    def __post_init__(self) -> None:
        if not self.key or not self.key.isidentifier():
            raise ValueError(f"field key must be an identifier, got {self.key!r}")
        if self.type not in _FIELD_TYPES:
            raise ValueError(f"field {self.key}: unknown type {self.type!r}")
        if self.type == "select" and not self.options:
            raise ValueError(f"field {self.key}: a select needs options")
        # the default must itself validate, so a schema bug fails at import time
        # (a required field may still default to empty: the user fills it in)
        coerce_field(self, self.default, check_required=False)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "default": self.default,
            "help": self.help,
            "placeholder": self.placeholder,
            "required": self.required,
        }
        if self.type == "select":
            d["options"] = [{"value": v, "label": lbl} for v, lbl in self.options]
        if self.type == "number":
            d["min"], d["max"], d["step"] = self.min, self.max, self.step
        return d


def coerce_field(f: Field, value: Any, *, check_required: bool = True) -> Any:
    """Coerce ``value`` for field ``f`` (as JSON from the UI would deliver it) and
    validate it.  Raises ``ValueError`` naming the field."""
    where = f.key
    if f.type in ("text", "textarea"):
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"{where}: expected text, got {type(value).__name__}")
        value = value.strip() if f.type == "text" else value
        if check_required and f.required and not value.strip():
            raise ValueError(f"{where}: required")
        return value
    if f.type == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "on", "off"):
            return value.lower() in ("true", "1", "on")
        raise ValueError(f"{where}: expected true/false, got {value!r}")
    if f.type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"{where}: expected a number, got {type(value).__name__}")
        try:
            num = float(value)
        except ValueError:
            raise ValueError(f"{where}: expected a number, got {value!r}") from None
        if num != num or num in (float("inf"), float("-inf")):
            raise ValueError(f"{where}: expected a finite number")
        if f.min is not None and num < f.min:
            raise ValueError(f"{where}: must be at least {f.min:g}, got {num:g}")
        if f.max is not None and num > f.max:
            raise ValueError(f"{where}: must be at most {f.max:g}, got {num:g}")
        if f.step == 1 or isinstance(f.default, int) and f.step is None:
            if num != int(num):
                raise ValueError(f"{where}: must be a whole number, got {num:g}")
            return int(num)
        return num
    if f.type == "select":
        values = [v for v, _ in f.options]
        if value not in values:
            raise ValueError(
                f"{where}: must be one of {', '.join(map(str, values))}, got {value!r}"
            )
        return value
    if f.type == "list":
        if value is None:
            value = []
        if isinstance(value, str):
            value = [line for line in value.splitlines()]
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{where}: expected a list of strings")
        items = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"{where}: expected strings, got {type(item).__name__}")
            item = item.strip()
            if item:
                items.append(item)
        if check_required and f.required and not items:
            raise ValueError(f"{where}: at least one entry is required")
        return items
    raise ValueError(f"{where}: unknown field type {f.type!r}")  # pragma: no cover


def schema_defaults(schema: Sequence[Field]) -> dict[str, Any]:
    """The settings dict an app gets before the user changes anything."""
    return {f.key: coerce_field(f, f.default, check_required=False) for f in schema}


def validate_settings(schema: Sequence[Field], data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge ``data`` over the schema defaults, coerce and validate every field.

    Unknown keys are rejected (the UI only sends known ones; a typo should be
    loud).  Returns a complete dict with every key of the schema.
    """
    data = dict(data or {})
    keys = {f.key for f in schema}
    unknown = sorted(set(data) - keys)
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
    out = {}
    for f in schema:
        out[f.key] = (
            coerce_field(f, data[f.key])
            if f.key in data
            else coerce_field(f, f.default, check_required=False)
        )
    return out


def schema_to_json(schema: Sequence[Field]) -> list[dict[str, Any]]:
    return [f.to_dict() for f in schema]


# --- fonts ---------------------------------------------------------------------------

FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"
FONTS_MANIFEST = FONTS_DIR / "fonts.json"

FontRole = Literal["serif", "sans", "mono"]


class FontError(RuntimeError):
    """A bundled font is missing or unreadable."""


class FontLibrary:
    """Loads the OFL fonts bundled in ``inkframe/fonts/`` by role and weight.

    ``fonts.json`` maps roles (``serif``, ``sans``, ``mono``) to files per
    weight (``regular`` and ``bold`` always exist; others such as ``medium``,
    ``semibold``, ``italic`` depend on the family).  Fonts are cached per
    ``(role, weight, size)``; ``ImageFont`` objects are not thread-safe for
    concurrent drawing, but all rendering happens on the single render thread.
    """

    def __init__(self, manifest: Path = FONTS_MANIFEST) -> None:
        self._manifest = Path(manifest)
        self._dir = self._manifest.parent
        self._roles: dict[str, dict[str, Any]] | None = None
        self._cache: dict[tuple[str, str, int], ImageFont.FreeTypeFont] = {}
        self._lock = threading.Lock()

    def _load_manifest(self) -> dict[str, dict[str, Any]]:
        if self._roles is None:
            if not self._manifest.is_file():
                raise FontError(
                    f"font manifest {self._manifest} is missing; the bundled fonts live in "
                    f"{self._dir} (see inkframe/fonts/README.md)"
                )
            data = json.loads(self._manifest.read_text(encoding="utf-8"))
            roles = data.get("roles")
            if not isinstance(roles, dict):
                raise FontError(f"{self._manifest}: missing 'roles'")
            self._roles = roles
        return self._roles

    def roles(self) -> list[str]:
        return list(self._load_manifest())

    def weights(self, role: str) -> list[str]:
        return list(self._role(role)["files"])

    def _role(self, role: str) -> dict[str, Any]:
        roles = self._load_manifest()
        if role not in roles:
            raise FontError(f"unknown font role {role!r}; roles: {', '.join(roles)}")
        return roles[role]

    def path(self, role: str, weight: str = "regular") -> Path:
        entry = self._role(role)
        files = entry.get("files", {})
        if weight not in files:
            if weight == "regular":
                raise FontError(f"font role {role!r} has no 'regular' weight")
            log.debug("font role %s has no weight %s; using regular", role, weight)
            weight = "regular"
            if weight not in files:
                raise FontError(f"font role {role!r} has no 'regular' weight")
        return self._dir / entry["dir"] / files[weight]

    def font(self, role: str, size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
        """A FreeType font for ``role``/``weight`` at pixel ``size`` (cached)."""
        size = int(size)
        if size < 4:
            raise ValueError(f"font size must be at least 4, got {size}")
        key = (role, weight, size)
        with self._lock:
            f = self._cache.get(key)
            if f is None:
                path = self.path(role, weight)
                if not path.is_file():
                    raise FontError(f"font file {path} is missing")
                f = ImageFont.truetype(str(path), size)
                self._cache[key] = f
            return f


# --- render context ------------------------------------------------------------------


@dataclass
class RenderContext:
    """Everything an app may use while rendering one card."""

    #: the app's validated settings (every schema key present)
    settings: dict[str, Any]
    #: local, timezone-aware time of the render
    now: datetime
    #: ``<data>/cache/<app_id>``, created; for API responses, downloaded assets
    cache_dir: Path
    fonts: FontLibrary = field(default_factory=FontLibrary)
    #: force a layout variant (0..variants-1); ``None`` lets the app choose
    variant: int | None = None
    #: seed for generative apps; ``None`` means "surprise me"
    seed: int | None = None
    #: ``False`` forbids network access (tests, offline mode): use the cache or
    #: raise ``AppError``/render an offline card
    online: bool = True
    #: timeout for any single HTTP request an app makes
    http_timeout: float = 10.0
    #: what the panel really shows; useful for a test card's labels
    measured_palette: Palette = MEASURED_DEFAULT
    #: for apps that are views over stored data (photos): the database, the
    #: data paths, the global settings snapshot and the item to show (a photo
    #: id chosen by the scheduler).  ``None`` for self-contained apps.
    db: Any = None
    paths: Any = None
    app_settings: Any = None
    item_id: str | None = None


# --- the protocol ------------------------------------------------------------------


@runtime_checkable
class App(Protocol):
    """What ``inkframe.apps.load_app`` expects of each module's ``APP``."""

    #: matches the module name and ``inkframe.apps.APP_IDS``
    id: str
    #: shown in the UI
    name: str
    #: one line for the Apps page
    description: str
    #: False: the render is already in exact palette colours (no dither pass)
    needs_dither: bool
    settings_schema: tuple[Field, ...]
    #: number of layout variants (``RenderContext.variant`` picks one)
    variants: int

    def render(self, ctx: RenderContext) -> Image.Image | np.ndarray:
        """Return a 600 x 400 RGB image or a (400, 600) palette-index frame."""


# --- normalisation --------------------------------------------------------------------


def render_to_frame(
    app: App,
    ctx: RenderContext,
    *,
    recipe: Mapping[str, Any] | None = None,
    render_palette: Palette | None = None,
) -> np.ndarray:
    """Run ``app.render`` and return a validated ``(400, 600)`` uint8 frame.

    * an index array is validated and returned as is;
    * an RGB image from a ``needs_dither = False`` app must use only
      ``NOMINAL`` colours (``ValueError`` otherwise: a renderer bug, not a
      runtime condition);
    * an RGB image from a ``needs_dither = True`` app is dithered with
      ``recipe`` (default: the settings' default recipe, passed in by the
      caller) against ``render_palette`` (default ``MEASURED_DEFAULT``).
    """
    result = app.render(ctx)
    if isinstance(result, np.ndarray):
        return validate_frame(result)
    if not isinstance(result, Image.Image):
        raise TypeError(f"app {app.id}: render() returned {type(result).__name__}")
    if result.size != (PANEL_WIDTH, PANEL_HEIGHT):
        raise ValueError(
            f"app {app.id}: render() returned a {result.size[0]}x{result.size[1]} image, "
            f"expected {PANEL_WIDTH}x{PANEL_HEIGHT}"
        )
    rgb = np.asarray(result.convert("RGB"), dtype=np.uint8)
    if not app.needs_dither:
        try:
            return rgb_to_indices_exact(rgb, NOMINAL)
        except ValueError as e:
            raise ValueError(f"app {app.id} (needs_dither=False) {e}") from None
    from inkframe.render.dither import dither  # Phase 2 engine; lazy so apps import without it

    r = dict(recipe or {})
    return dither(
        rgb,
        render_palette or MEASURED_DEFAULT,
        str(r.get("method", "atkinson")),
        serpentine=bool(r.get("serpentine", True)),
        strength=float(r.get("strength", 0.85)),
        distance=str(r.get("distance", "lab")),
    )
