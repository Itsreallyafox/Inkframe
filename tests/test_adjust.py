"""Pre-dither adjustments: neutrality, each knob's effect, the fixed order."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageEnhance, ImageFilter

from inkframe.render import adjust
from tests.conftest import gradient_image


@pytest.fixture(scope="module")
def photo() -> Image.Image:
    return gradient_image(600, 400)


def _arr(im: Image.Image) -> np.ndarray:
    return np.asarray(im, dtype=np.float64)


def test_neutral_is_pixel_identical_copy(photo: Image.Image) -> None:
    before = np.asarray(photo).copy()
    out = adjust.apply(photo, brightness=1.0, contrast=1.0, saturation=1.0, gamma=1.0, sharpen=0.0)
    assert out is not photo and out.mode == "RGB" and out.size == photo.size
    assert np.array_equal(np.asarray(out), before)
    assert np.array_equal(np.asarray(photo), before)  # input untouched
    assert np.array_equal(np.asarray(adjust.apply(photo)), before)  # defaults are neutral
    assert np.array_equal(np.asarray(adjust.apply_recipe(photo, adjust.NEUTRAL)), before)
    assert adjust.is_neutral()
    # a sharpen too small to reach 1 % is neutral as well
    assert np.array_equal(np.asarray(adjust.apply(photo, sharpen=0.004)), before)


def test_non_rgb_is_refused() -> None:
    with pytest.raises(ValueError, match="RGB"):
        adjust.apply(Image.new("L", (4, 4)))


def test_brightness(photo: Image.Image) -> None:
    base = _arr(photo).mean()
    assert _arr(adjust.apply(photo, brightness=1.4)).mean() > base + 10
    assert _arr(adjust.apply(photo, brightness=0.6)).mean() < base - 10


def test_contrast(photo: Image.Image) -> None:
    base = _arr(photo.convert("L")).std()
    assert _arr(adjust.apply(photo, contrast=1.4).convert("L")).std() > base * 1.2
    assert _arr(adjust.apply(photo, contrast=0.6).convert("L")).std() < base * 0.8


def _chroma(im: Image.Image) -> float:
    a = _arr(im)
    return float((a.max(axis=2) - a.min(axis=2)).mean())


def test_saturation(photo: Image.Image) -> None:
    base = _chroma(photo)
    assert _chroma(adjust.apply(photo, saturation=1.5)) > base * 1.2
    assert _chroma(adjust.apply(photo, saturation=0.5)) < base * 0.7
    grey = Image.new("RGB", (32, 32), (128, 128, 128))
    assert np.array_equal(np.asarray(adjust.apply(grey, saturation=1.5)), np.asarray(grey))


def test_gamma_lut() -> None:
    assert np.array_equal(adjust.gamma_lut(1.0), np.arange(256, dtype=np.uint8))
    lut = adjust.gamma_lut(2.0)
    assert lut.dtype == np.uint8 and lut.shape == (256,)
    assert lut[0] == 0 and lut[255] == 255
    assert lut[64] == round(255 * (64 / 255) ** 0.5)  # 128: mid-tones lifted
    assert np.all(np.diff(lut.astype(int)) >= 0)
    assert adjust.gamma_lut(0.5)[128] == round(255 * (128 / 255) ** 2)  # darkened
    with pytest.raises(ValueError):
        adjust.gamma_lut(0)


def test_gamma_applies_the_lut_per_channel(photo: Image.Image) -> None:
    out = np.asarray(adjust.apply(photo, gamma=1.8))
    lut = adjust.gamma_lut(1.8)
    assert np.array_equal(out, lut[np.asarray(photo)])


def _detail(im: Image.Image) -> float:
    a = _arr(im.convert("L"))
    return float(np.abs(np.diff(a, axis=1)).mean() + np.abs(np.diff(a, axis=0)).mean())


def test_sharpen() -> None:
    y, x = np.mgrid[0:200, 0:300]
    checks = (((x // 10) + (y // 10)) % 2 * 160 + 40).astype(np.uint8)
    soft = Image.fromarray(np.stack([checks] * 3, axis=-1)).filter(ImageFilter.GaussianBlur(2))
    assert _detail(adjust.apply(soft, sharpen=1.5)) > _detail(soft) * 1.2
    expected = soft.filter(ImageFilter.UnsharpMask(radius=1.5, percent=150, threshold=2))
    assert np.array_equal(np.asarray(adjust.apply(soft, sharpen=1.5)), np.asarray(expected))


def test_order_is_brightness_contrast_saturation_gamma_sharpen(photo: Image.Image) -> None:
    knobs = dict(brightness=1.2, contrast=0.8, saturation=1.3, gamma=1.6, sharpen=0.7)
    ref = ImageEnhance.Brightness(photo).enhance(1.2)
    ref = ImageEnhance.Contrast(ref).enhance(0.8)
    ref = ImageEnhance.Color(ref).enhance(1.3)
    ref = ref.point(adjust.gamma_lut(1.6).tolist() * 3)
    ref = ref.filter(ImageFilter.UnsharpMask(radius=1.5, percent=70, threshold=2))
    out = adjust.apply(photo, **knobs)
    assert np.array_equal(np.asarray(out), np.asarray(ref))
    recipe = {**knobs, "method": "atkinson", "palette": "measured"}
    assert np.array_equal(np.asarray(adjust.apply_recipe(photo, recipe)), np.asarray(ref))
    swapped = ImageEnhance.Contrast(photo).enhance(0.8)
    swapped = ImageEnhance.Brightness(swapped).enhance(1.2)
    assert not np.array_equal(
        np.asarray(adjust.apply(photo, brightness=1.2, contrast=0.8)), np.asarray(swapped)
    )
