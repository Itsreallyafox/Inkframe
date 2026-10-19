"""User settings: typed, validated, stored as JSON at ``<data>/settings.json``.

The on-disk model is a tree of frozen dataclasses.  Every instance validates
itself on construction, so a ``Settings`` object that exists is valid; build
modified copies with ``dataclasses.replace`` or ``Settings.merged(patch)``.

Loading is forward compatible: keys missing from the file get their defaults,
unknown keys are ignored with a warning.  Updates coming from the API are
strict: unknown keys are rejected.  Every validation failure is a
``ValueError`` whose message names the offending field, e.g.
``panel.refresh_floor_seconds must be between 30 and 86400, got 10``.

``SettingsStore`` owns the current snapshot for the running process.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from inkframe.display.palette import MEASURED_DEFAULT, NAMES, NOMINAL, VALID_ROTATIONS, Palette
from inkframe.paths import atomic_write_bytes

log = logging.getLogger("inkframe.config")

#: panel-care limits (plan section 2); the scheduler enforces them in code too
MIN_REFRESH_FLOOR_SECONDS = 30
MIN_SHOW_NOW_SECONDS = 30
MAX_KEEPALIVE_HOURS = 23
_MAX_SECONDS = 24 * 3600
_MAX_SLOT_MINUTES = 24 * 60
_MAX_SLOTS = 32

#: the recipe new uploads get (plan section 13); per-photo recipes override it
DEFAULT_RECIPE: Mapping[str, Any] = MappingProxyType(
    {
        "method": "atkinson",
        "serpentine": True,
        "strength": 0.85,
        "distance": "lab",
        "brightness": 1.0,
        "contrast": 1.0,
        "saturation": 1.1,
        "gamma": 1.0,
        "sharpen": 0.0,
        "palette": "measured",
    }
)
#: inclusive (min, max) for each numeric recipe knob
RECIPE_RANGES: Mapping[str, tuple[float, float]] = MappingProxyType(
    {
        "strength": (0.5, 1.0),
        "brightness": (0.5, 1.5),
        "contrast": (0.5, 1.5),
        "saturation": (0.5, 1.5),
        "gamma": (0.5, 2.0),
        "sharpen": (0.0, 2.0),
    }
)
RECIPE_DISTANCES = ("rgb", "lab")
RECIPE_PALETTES = ("nominal", "measured")

_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_APP_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")


# --- field validators ---------------------------------------------------------


def _bool(path: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be true or false, got {value!r}")
    return value


def _int(path: str, value: object, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be an integer, got {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{path} must be a whole number, got {value!r}")
        value = int(value)
    if not lo <= value <= hi:
        raise ValueError(f"{path} must be between {lo} and {hi}, got {value}")
    return value


def _number(path: str, value: object, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a number, got {value!r}")
    v = float(value)
    if not (math.isfinite(v) and lo <= v <= hi):
        raise ValueError(f"{path} must be between {lo} and {hi}, got {value!r}")
    return v


def _choice(path: str, value: object, choices: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{path} must be one of {', '.join(choices)}, got {value!r}")
    return value


def parse_hhmm(text: object) -> dtime:
    """Parse a zero-padded 24-hour ``"HH:MM"`` string; ``ValueError`` otherwise."""
    m = _HHMM.fullmatch(text) if isinstance(text, str) else None
    if m is None:
        raise ValueError(f"expected a 24-hour time 'HH:MM', got {text!r}")
    return dtime(int(m.group(1)), int(m.group(2)))


def _freeze(value: Any) -> Any:
    """Read-only deep copy: mappings become ``MappingProxyType``, lists tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    """Inverse of ``_freeze``: plain, JSON-ready dicts and lists (always a deep copy)."""
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    return value


def deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``base`` with ``patch`` applied: nested mappings merge key by key,
    anything else (scalars, lists) replaces.  Neither argument is modified."""
    out: dict[str, Any] = {k: _thaw(v) for k, v in base.items()}
    for k, v in patch.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = _thaw(v)
    return out


def _pick(path: str, data: object, allowed: Sequence[str], strict: bool) -> dict[str, Any]:
    """The ``allowed`` keys of mapping ``data``; unknown keys raise (strict) or
    are dropped with a warning (loading a file written by a newer version)."""
    if not isinstance(data, Mapping):
        raise ValueError(f"{path} must be an object, got {type(data).__name__}")
    unknown = sorted(str(k) for k in data if k not in allowed)
    if unknown:
        if strict:
            raise ValueError(f"{path} has unknown key(s): {', '.join(unknown)}")
        log.warning("%s: ignoring unknown key(s): %s", path, ", ".join(unknown))
    return {k: data[k] for k in allowed if k in data}


# --- recipe ------------------------------------------------------------------


def validate_recipe(recipe: object, where: str = "recipe") -> dict[str, Any]:
    """Check a *complete* render recipe and return a normalised copy.

    Numbers come back as ``float``.  The method name is only checked for being
    a non-empty string; the method registry (Phase 2) owns the list of names.
    Raises ``ValueError`` naming the first bad field.
    """
    data = _pick(where, recipe, tuple(DEFAULT_RECIPE), strict=True)
    missing = [k for k in DEFAULT_RECIPE if k not in data]
    if missing:
        raise ValueError(f"{where} is missing key(s): {', '.join(missing)}")
    method = data["method"]
    if not isinstance(method, str) or not method.strip():
        raise ValueError(f"{where}.method must be a non-empty string, got {method!r}")
    out: dict[str, Any] = {
        "method": method.strip(),
        "serpentine": _bool(f"{where}.serpentine", data["serpentine"]),
    }
    for key in DEFAULT_RECIPE:
        if key in RECIPE_RANGES:
            lo, hi = RECIPE_RANGES[key]
            out[key] = _number(f"{where}.{key}", data[key], lo, hi)
    out["distance"] = _choice(f"{where}.distance", data["distance"], RECIPE_DISTANCES)
    out["palette"] = _choice(f"{where}.palette", data["palette"], RECIPE_PALETTES)
    return {k: out[k] for k in DEFAULT_RECIPE}


# --- sections ------------------------------------------------------------------


@dataclass(frozen=True)
class DisplaySettings:
    """How frames reach the panel."""

    #: 90 = the vendor driver's orientation, 270 = frame mounted the other way up
    rotation: int = 90
    #: flood the panel white before every refresh (less ghosting, twice as slow)
    clear_before_refresh: bool = False

    def __post_init__(self) -> None:
        rotation = _int("display.rotation", self.rotation, 0, 359)
        if rotation not in VALID_ROTATIONS:
            raise ValueError(
                f"display.rotation must be one of {', '.join(map(str, VALID_ROTATIONS))}, "
                f"got {rotation}"
            )
        object.__setattr__(self, "rotation", rotation)
        _bool("display.clear_before_refresh", self.clear_before_refresh)

    def to_dict(self) -> dict[str, Any]:
        return {"rotation": self.rotation, "clear_before_refresh": self.clear_before_refresh}


@dataclass(frozen=True)
class PanelSettings:
    """Panel-care timing (plan section 2)."""

    #: minimum time between scheduled refreshes (at most ``keepalive_hours``)
    refresh_floor_seconds: int = 180
    #: "show now" may bypass the floor but never refreshes faster than this
    show_now_min_seconds: int = 30
    #: refresh at least this often, even when paused or in quiet hours
    keepalive_hours: int = 20

    def __post_init__(self) -> None:
        floor = _int(
            "panel.refresh_floor_seconds",
            self.refresh_floor_seconds,
            MIN_REFRESH_FLOOR_SECONDS,
            _MAX_SECONDS,
        )
        show_now = _int(
            "panel.show_now_min_seconds",
            self.show_now_min_seconds,
            MIN_SHOW_NOW_SECONDS,
            _MAX_SECONDS,
        )
        if show_now > floor:
            raise ValueError(
                f"panel.show_now_min_seconds ({show_now}) must not exceed "
                f"panel.refresh_floor_seconds ({floor})"
            )
        keepalive = _int("panel.keepalive_hours", self.keepalive_hours, 1, MAX_KEEPALIVE_HOURS)
        if floor > keepalive * 3600:
            # the keep-alive is a refresh too: a longer floor would hold it back
            raise ValueError(
                f"panel.refresh_floor_seconds ({floor}) must not exceed "
                f"panel.keepalive_hours ({keepalive} h = {keepalive * 3600} s)"
            )
        object.__setattr__(self, "refresh_floor_seconds", floor)
        object.__setattr__(self, "show_now_min_seconds", show_now)
        object.__setattr__(self, "keepalive_hours", keepalive)

    def to_dict(self) -> dict[str, Any]:
        return {
            "refresh_floor_seconds": self.refresh_floor_seconds,
            "show_now_min_seconds": self.show_now_min_seconds,
            "keepalive_hours": self.keepalive_hours,
        }


@dataclass(frozen=True)
class QuietHours:
    """No scheduled refreshes between ``start`` and ``end`` (local time).

    The window may wrap midnight (``23:00``-``07:00``).  ``days`` are the
    weekdays (Mon=0 .. Sun=6) on which a window *starts*: with Sunday listed,
    Sunday 23:00 through Monday 07:00 is quiet.  ``start`` and ``end`` must
    differ.
    """

    enabled: bool = True
    start: str = "23:00"
    end: str = "07:00"
    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)

    def __post_init__(self) -> None:
        _bool("quiet_hours.enabled", self.enabled)
        for name in ("start", "end"):
            try:
                parse_hhmm(getattr(self, name))
            except ValueError as e:
                raise ValueError(f"quiet_hours.{name}: {e}") from None
        if self.start == self.end:
            raise ValueError("quiet_hours.start and quiet_hours.end must differ")
        if not isinstance(self.days, (list, tuple)):
            raise ValueError(f"quiet_hours.days must be a list of weekdays 0..6, got {self.days!r}")
        days = sorted({_int(f"quiet_hours.days[{i}]", d, 0, 6) for i, d in enumerate(self.days)})
        object.__setattr__(self, "days", tuple(days))

    def start_time(self) -> dtime:
        return parse_hhmm(self.start)

    def end_time(self) -> dtime:
        return parse_hhmm(self.end)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "start": self.start,
            "end": self.end,
            "days": list(self.days),
        }


@dataclass(frozen=True)
class PaletteSettings:
    """The panel's real colours (tuned from the palette test card)."""

    measured: Palette = MEASURED_DEFAULT
    #: dither against ``measured`` (True) or the nominal primaries (False)
    use_measured: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.measured, Palette):
            raise ValueError(f"palette.measured must be a Palette, got {self.measured!r}")
        if self.measured.name != "measured":
            object.__setattr__(self, "measured", Palette("measured", self.measured.rgb))
        _bool("palette.use_measured", self.use_measured)

    def to_dict(self) -> dict[str, Any]:
        return {"measured": self.measured.as_dict(), "use_measured": self.use_measured}


def _parse_palette(data: object, strict: bool) -> Palette:
    colors = _pick("palette.measured", data, NAMES, strict)
    out: dict[str, tuple[int, int, int]] = {}
    for name in NAMES:
        path = f"palette.measured.{name}"
        value = colors.get(name)
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError(f"{path} must be an [r, g, b] list, got {value!r}")
        r, g, b = (_int(f"{path}[{i}]", v, 0, 255) for i, v in enumerate(value))
        out[name] = (r, g, b)
    return Palette.from_dict("measured", out)


@dataclass(frozen=True)
class RotationSlot:
    """One rotation entry: show ``app_id`` for ``duration_minutes``.

    ``options`` is opaque here and owned by the app / scheduler (the photos
    slot uses ``{"dwell_minutes": int, "order": "shuffle"|"sequential"}``).
    It is stored read-only; ``to_dict`` returns a plain copy.
    """

    app_id: str
    duration_minutes: int
    enabled: bool = True
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.app_id, str) or _APP_ID.fullmatch(self.app_id) is None:
            raise ValueError(
                f"app_id must be a lowercase identifier (a-z, 0-9, _), got {self.app_id!r}"
            )
        object.__setattr__(
            self,
            "duration_minutes",
            _int("duration_minutes", self.duration_minutes, 1, _MAX_SLOT_MINUTES),
        )
        _bool("enabled", self.enabled)
        if not isinstance(self.options, Mapping) or not all(
            isinstance(k, str) for k in self.options
        ):
            raise ValueError(f"options must be an object with string keys, got {self.options!r}")
        try:
            json.dumps(_thaw(self.options), allow_nan=False)
        except (TypeError, ValueError) as e:
            raise ValueError(f"options must be JSON-serialisable: {e}") from None
        object.__setattr__(self, "options", _freeze(self.options))

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "enabled": self.enabled,
            "duration_minutes": self.duration_minutes,
            "options": _thaw(self.options),
        }


_SLOT_KEYS = ("app_id", "enabled", "duration_minutes", "options")


def _parse_rotation(data: object, strict: bool) -> tuple[RotationSlot, ...]:
    if not isinstance(data, (list, tuple)):
        raise ValueError(f"rotation must be a list of slots, got {type(data).__name__}")
    if len(data) > _MAX_SLOTS:
        raise ValueError(f"rotation has {len(data)} slots; at most {_MAX_SLOTS} are allowed")
    slots = []
    for i, item in enumerate(data):
        path = f"rotation[{i}]"
        kw = _pick(path, item, _SLOT_KEYS, strict)
        missing = [k for k in ("app_id", "duration_minutes") if k not in kw]
        if missing:
            raise ValueError(f"{path} is missing key(s): {', '.join(missing)}")
        try:
            slots.append(RotationSlot(**kw))
        except ValueError as e:
            raise ValueError(f"{path}.{e}") from None
    return tuple(slots)


def _default_rotation() -> tuple[RotationSlot, ...]:
    return (
        RotationSlot("photos", 10, options={"dwell_minutes": 3, "order": "shuffle"}),
        RotationSlot("weather", 3),
        RotationSlot("headlines", 3),
        RotationSlot("quotes", 2),
        RotationSlot("patterns", 2),
    )


# --- the whole tree ----------------------------------------------------------------

_TOP_KEYS = ("display", "panel", "quiet_hours", "palette", "default_recipe", "rotation", "paused")


@dataclass(frozen=True)
class Settings:
    """All user settings.  Immutable and always valid; see module docstring."""

    display: DisplaySettings = field(default_factory=DisplaySettings)
    panel: PanelSettings = field(default_factory=PanelSettings)
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    palette: PaletteSettings = field(default_factory=PaletteSettings)
    #: read-only mapping; ``dict(settings.default_recipe)`` for a mutable copy
    default_recipe: Mapping[str, Any] = field(default_factory=lambda: DEFAULT_RECIPE)
    rotation: tuple[RotationSlot, ...] = field(default_factory=_default_rotation)
    #: rotation paused from the UI (keep-alive refreshes still happen)
    paused: bool = False

    def __post_init__(self) -> None:
        for name, cls in (
            ("display", DisplaySettings),
            ("panel", PanelSettings),
            ("quiet_hours", QuietHours),
            ("palette", PaletteSettings),
        ):
            if not isinstance(getattr(self, name), cls):
                raise ValueError(f"{name} must be a {cls.__name__}")
        recipe = validate_recipe(self.default_recipe, "default_recipe")
        object.__setattr__(self, "default_recipe", MappingProxyType(recipe))
        if not isinstance(self.rotation, (list, tuple)) or not all(
            isinstance(s, RotationSlot) for s in self.rotation
        ):
            raise ValueError("rotation must be a sequence of RotationSlot")
        if len(self.rotation) > _MAX_SLOTS:
            raise ValueError(f"rotation has {len(self.rotation)} slots; at most {_MAX_SLOTS}")
        object.__setattr__(self, "rotation", tuple(self.rotation))
        _bool("paused", self.paused)

    # --- palettes ---------------------------------------------------------
    def measured_palette(self) -> Palette:
        """What the panel actually shows; use it to draw honest previews."""
        return self.palette.measured

    def render_palette(self) -> Palette:
        """The palette to dither against: measured if enabled, else nominal."""
        return self.palette.measured if self.palette.use_measured else NOMINAL

    # --- (de)serialisation ------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-ready dict; the exact shape of ``settings.json`` and the API."""
        return {
            "display": self.display.to_dict(),
            "panel": self.panel.to_dict(),
            "quiet_hours": self.quiet_hours.to_dict(),
            "palette": self.palette.to_dict(),
            "default_recipe": dict(self.default_recipe),
            "rotation": [s.to_dict() for s in self.rotation],
            "paused": self.paused,
        }

    @classmethod
    def from_dict(cls, data: object, *, strict: bool = True) -> Settings:
        """Build from a (possibly partial) dict; missing keys take defaults.

        ``strict`` rejects unknown keys at any level (API input); otherwise they
        are logged and ignored (a file written by a newer version).
        """
        if not isinstance(data, Mapping):
            raise ValueError(f"settings must be an object, got {type(data).__name__}")
        top = _pick("settings", deep_merge(_DEFAULTS, data), _TOP_KEYS, strict)
        display = _pick("display", top["display"], ("rotation", "clear_before_refresh"), strict)
        panel = _pick(
            "panel",
            top["panel"],
            ("refresh_floor_seconds", "show_now_min_seconds", "keepalive_hours"),
            strict,
        )
        quiet = _pick(
            "quiet_hours", top["quiet_hours"], ("enabled", "start", "end", "days"), strict
        )
        palette = _pick("palette", top["palette"], ("measured", "use_measured"), strict)
        recipe = top["default_recipe"]
        if isinstance(recipe, Mapping) and not strict:
            recipe = _pick("default_recipe", recipe, tuple(DEFAULT_RECIPE), strict=False)
        return cls(
            display=DisplaySettings(**display),
            panel=PanelSettings(**panel),
            quiet_hours=QuietHours(**quiet),
            palette=PaletteSettings(
                measured=_parse_palette(palette["measured"], strict),
                use_measured=palette["use_measured"],
            ),
            default_recipe=recipe,
            rotation=_parse_rotation(top["rotation"], strict),
            paused=top["paused"],
        )

    def merged(self, patch: object) -> Settings:
        """A new ``Settings`` with a full or partial dict deep-merged over this
        one (lists such as ``rotation`` and ``quiet_hours.days`` are replaced
        whole), validated strictly."""
        if not isinstance(patch, Mapping):
            raise ValueError(f"settings update must be an object, got {type(patch).__name__}")
        return Settings.from_dict(deep_merge(self.to_dict(), patch), strict=True)


_DEFAULTS: dict[str, Any] = Settings().to_dict()


# --- persistence -------------------------------------------------------------------


def load_settings(path: Path) -> Settings:
    """Read ``path``; a missing file yields the defaults.

    Raises ``ValueError`` if the file is not valid JSON or fails validation.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON: {e}") from None
    return Settings.from_dict(data, strict=False)


def save_settings(settings: Settings, path: Path) -> None:
    """Write ``settings`` to ``path`` atomically (temp file + ``os.replace``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(settings.to_dict(), indent=2) + "\n"
    atomic_write_bytes(path, text.encode("utf-8"))


class SettingsStore:
    """Thread-safe owner of the process's current settings and their file.

    ``get()`` is lock-free and returns an immutable snapshot.  Writers
    (``replace``/``update``/``patch``) validate, save, then swap the snapshot
    under one lock, so the file and memory never disagree and concurrent
    updates never lose each other's changes.  Callbacks passed to ``update``
    must not call back into the store.

    On start-up a missing file is created with the defaults; an unreadable or
    invalid one is moved aside to ``settings.json.invalid`` (and logged) so
    the frame still boots.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._settings = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> Settings:
        try:
            settings = load_settings(self._path)
        except ValueError as e:
            backup = self._path.with_name(self._path.name + ".invalid")
            os.replace(self._path, backup)
            log.error("invalid settings file (%s); moved it to %s and using defaults", e, backup)
            settings = Settings()
            save_settings(settings, self._path)
            return settings
        if not self._path.exists():
            save_settings(settings, self._path)
            log.info("wrote default settings to %s", self._path)
        return settings

    def get(self) -> Settings:
        """The current settings snapshot."""
        return self._settings

    def replace(self, new: Settings) -> Settings:
        """Validate, persist and install ``new``; returns the installed snapshot."""
        return self.update(lambda _old: new)

    def update(self, fn: Callable[[Settings], Settings]) -> Settings:
        """Atomically apply ``fn(current) -> new``; ``fn`` may raise ``ValueError``
        to abort, in which case nothing changes."""
        with self._lock:
            new = fn(self._settings)
            if not isinstance(new, Settings):
                raise TypeError(f"settings update must produce Settings, got {type(new).__name__}")
            validated = Settings.from_dict(new.to_dict(), strict=True)
            save_settings(validated, self._path)
            self._settings = validated
        return validated

    def patch(self, patch: object) -> Settings:
        """Deep-merge a full or partial settings dict (the API's PUT body)."""
        return self.update(lambda current: current.merged(patch))
