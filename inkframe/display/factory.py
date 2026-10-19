"""Build the configured ``Display`` (``INKFRAME_DISPLAY=mock|epd``)."""

from __future__ import annotations

from pathlib import Path

from inkframe.config import SettingsStore
from inkframe.display.base import Display, DisplayError
from inkframe.display.mock import MockDisplay

DISPLAY_KINDS = ("mock", "epd")


def create_display(kind: str, *, data_dir: Path, settings_store: SettingsStore) -> Display:
    """Return a new, asleep display of ``kind``.

    Both kinds read the packing rotation from the settings store on every
    refresh, so a settings change applies to the next frame.  The mock renders
    ``last_frame.png`` in the *measured* palette because that is what the panel
    physically shows, whichever palette a recipe dithered against.

    ``epd`` imports the hardware driver lazily: it needs ``spidev`` and
    ``gpiozero`` and only works on the Pi.  Unknown kinds raise ``ValueError``.
    """
    kind = kind.strip().lower()

    def rotation() -> int:
        return settings_store.get().display.rotation

    if kind == "mock":
        return MockDisplay(
            Path(data_dir),
            palette_fn=lambda: settings_store.get().measured_palette(),
            rotation_fn=rotation,
        )
    if kind == "epd":
        try:
            from inkframe.display.epd4in0e import EPD4in0e
        except ImportError as e:
            raise DisplayError(
                "INKFRAME_DISPLAY=epd needs the panel driver's spidev and gpiozero modules "
                f"and only works on the Raspberry Pi ({e}); use INKFRAME_DISPLAY=mock elsewhere"
            ) from e
        return EPD4in0e(rotation_fn=rotation)
    raise ValueError(f"unknown display kind {kind!r}; expected one of {', '.join(DISPLAY_KINDS)}")
