"""Pre-dither adjustments: brightness, contrast, saturation, gamma, sharpen.

Applied to the fitted 600 x 400 photo in RGB, before the dither (plan
section 5).  The implementation is deliberately cheap on the Pi: Pillow
``ImageEnhance`` for the first three, one 256-entry numpy lookup table for
gamma, and Pillow's ``UnsharpMask`` for sharpen.  The order is fixed:

1. brightness (``ImageEnhance.Brightness``: blend with black),
2. contrast (``ImageEnhance.Contrast``: blend with the mean grey),
3. saturation (``ImageEnhance.Color``: blend with the greyscale image),
4. gamma (``out = 255 * (in / 255) ** (1 / gamma)``: gamma > 1 lifts mid-tones),
5. sharpen (``UnsharpMask(radius=1.5, percent=round(sharpen * 100), threshold=2)``).

Neutral values (1, 1, 1, 1, 0) skip their stage entirely, so a neutral
recipe returns a pixel-identical copy.  The input image is never modified.
Ranges are validated by ``inkframe.config.validate_recipe``, not here.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from typing import Any

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

#: knob -> neutral value; also the recipe keys this module reads
NEUTRAL: Mapping[str, float] = {
    "brightness": 1.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "gamma": 1.0,
    "sharpen": 0.0,
}
SHARPEN_RADIUS = 1.5
SHARPEN_THRESHOLD = 2


@functools.lru_cache(maxsize=32)
def _gamma_table(gamma: float) -> tuple[int, ...]:
    v = np.arange(256, dtype=np.float64) / 255.0
    lut = np.clip(np.rint(255.0 * np.power(v, 1.0 / gamma)), 0, 255).astype(np.uint8)
    return tuple(int(x) for x in lut)


def gamma_lut(gamma: float) -> np.ndarray:
    """The 256-entry uint8 table ``round(255 * (i / 255) ** (1 / gamma))``."""
    if not gamma > 0:
        raise ValueError(f"gamma must be positive, got {gamma!r}")
    return np.array(_gamma_table(float(gamma)), dtype=np.uint8)


def is_neutral(
    *,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    gamma: float = 1.0,
    sharpen: float = 0.0,
) -> bool:
    """True when ``apply`` with these values changes nothing."""
    return (
        brightness == 1.0
        and contrast == 1.0
        and saturation == 1.0
        and gamma == 1.0
        and int(round(sharpen * 100)) <= 0
    )


def apply(
    image: Image.Image,
    *,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    gamma: float = 1.0,
    sharpen: float = 0.0,
) -> Image.Image:
    """Return ``image`` (``RGB``) with the adjustments applied, in the order
    brightness, contrast, saturation, gamma, sharpen.  Always a new image."""
    if image.mode != "RGB":
        raise ValueError(f"adjust expects an RGB image, got mode {image.mode!r}")
    out = image
    if brightness != 1.0:
        out = ImageEnhance.Brightness(out).enhance(float(brightness))
    if contrast != 1.0:
        out = ImageEnhance.Contrast(out).enhance(float(contrast))
    if saturation != 1.0:
        out = ImageEnhance.Color(out).enhance(float(saturation))
    if gamma != 1.0:
        out = out.point(list(_gamma_table(float(gamma))) * 3)
    percent = int(round(sharpen * 100))
    if percent > 0:
        out = out.filter(
            ImageFilter.UnsharpMask(
                radius=SHARPEN_RADIUS, percent=percent, threshold=SHARPEN_THRESHOLD
            )
        )
    return out.copy() if out is image else out


def apply_recipe(image: Image.Image, recipe: Mapping[str, Any]) -> Image.Image:
    """``apply`` with the adjustment knobs of a (validated) recipe."""
    return apply(image, **{k: float(recipe.get(k, v)) for k, v in NEUTRAL.items()})
