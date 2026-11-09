"""Photo pipeline: fit geometry, render, recipe keys, the fit LRU and the render cache."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import os
import platform
import re
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from inkframe.config import DEFAULT_RECIPE, Settings
from inkframe.db import Database, Photo
from inkframe.display.palette import (
    MEASURED_DEFAULT,
    NOMINAL,
    PANEL_HEIGHT,
    PANEL_WIDTH,
    WHITE,
    Palette,
    pack_frame,
)
from inkframe.paths import DataPaths
from inkframe.render import adjust, pipeline
from inkframe.render import cache as cache_module
from inkframe.render.cache import (
    MAX_RENDERS_PER_PHOTO,
    RenderCache,
    decode_png,
    encode_png,
    valid_key,
)
from inkframe.render.dither import METHOD_IDS, METHODS, METHODS_BY_ID, dither
from inkframe.render.pipeline import (
    FULL_BOX,
    RecipeError,
    complete_recipe,
    crop_box,
    effective_recipe,
    fit_geometry,
    fit_image,
    fit_master,
    recipe_key,
    render_frame,
    resolve_render_palette,
    validate_crop,
)
from tests.conftest import PhotoFactory, gradient_image

WHITE_RGB = (255, 255, 255)
RECIPE = complete_recipe(DEFAULT_RECIPE)


@pytest.fixture(autouse=True)
def _fresh_fit_cache() -> None:
    pipeline.clear_fit_cache()


# --- fit -----------------------------------------------------------------------------------


def _bars(fit: Image.Image, box: tuple[int, int, int, int]) -> np.ndarray:
    a = np.asarray(fit)
    mask = np.ones(a.shape[:2], bool)
    x0, y0, x1, y1 = box
    mask[y0:y1, x0:x1] = False
    return a[mask]


def test_three_two_fills_the_panel_exactly() -> None:
    master = gradient_image(1800, 1200)
    fit = fit_image(master)
    assert fit.box == FULL_BOX == (0, 0, 600, 400)
    assert fit.image.size == (600, 400) and fit.image.mode == "RGB"
    expected = master.resize((600, 400), Image.Resampling.LANCZOS)
    assert np.array_equal(np.asarray(fit.image), np.asarray(expected))
    assert np.array_equal(np.asarray(fit_master(master)), np.asarray(expected))


def test_portrait_is_letterboxed_left_and_right_never_rotated() -> None:
    master = Image.new("RGB", (800, 1200), (200, 30, 40))
    fit = fit_image(master)
    assert fit.box == (166, 0, 433, 400)  # 267 px wide, centred
    a = np.asarray(fit.image)
    assert np.all(a[:, :166] == WHITE_RGB) and np.all(a[:, 433:] == WHITE_RGB)
    assert np.all(a[:, 168:431] == (200, 30, 40))
    assert np.all(_bars(fit.image, fit.box) == WHITE_RGB)


def test_four_three_is_pillarboxed() -> None:
    # 4:3 is narrower than the panel's 3:2, so its bars are left and right
    fit = fit_image(Image.new("RGB", (1600, 1200), (10, 90, 30)))
    assert fit.box == (33, 0, 566, 400)
    a = np.asarray(fit.image)
    assert np.all(a[:, :33] == WHITE_RGB) and np.all(a[:, 566:] == WHITE_RGB)
    assert np.all(a[:, 35:564] == (10, 90, 30))


def test_wide_images_get_top_and_bottom_bars() -> None:
    fit = fit_image(Image.new("RGB", (1920, 1080), (10, 20, 200)))
    assert fit.box == (0, 31, 600, 369)  # 16:9 -> 600 x 338
    a = np.asarray(fit.image)
    assert np.all(a[:31] == WHITE_RGB) and np.all(a[369:] == WHITE_RGB)
    assert np.all(a[33:367] == (10, 20, 200))
    pano = fit_image(Image.new("RGB", (3000, 500), (0, 0, 0)))
    assert pano.box == (0, 150, 600, 250)


def test_small_images_are_scaled_up_to_fit() -> None:
    assert fit_image(Image.new("RGB", (300, 200), (1, 2, 3))).box == FULL_BOX
    assert fit_geometry(30, 40) == (150, 0, 450, 400)
    assert fit_geometry(1, 1) == (100, 0, 500, 400)
    with pytest.raises(ValueError):
        fit_geometry(0, 10)


def test_fit_converts_other_modes() -> None:
    fit = fit_image(Image.new("L", (600, 400), 77))
    assert fit.image.mode == "RGB" and fit.image.getpixel((10, 10)) == (77, 77, 77)


# --- crops ----------------------------------------------------------------------------------

RED_Q, GREEN_Q, BLUE_Q, YELLOW_Q = (220, 30, 30), (30, 200, 60), (30, 60, 220), (240, 220, 40)


def quadrants(width: int, height: int) -> Image.Image:
    """A master of four flat colours: red top-left, green top-right, blue
    bottom-left, yellow bottom-right, so a crop shows where it came from."""
    a = np.zeros((height, width, 3), dtype=np.uint8)
    hx, hy = width // 2, height // 2
    a[:hy, :hx] = RED_Q
    a[:hy, hx:] = GREEN_Q
    a[hy:, :hx] = BLUE_Q
    a[hy:, hx:] = YELLOW_Q
    return Image.fromarray(a)


#: the fill crop of a 1200 x 1800 portrait master: full width, the middle 3:2 band
PORTRAIT_FILL = {"x": 0.0, "y": 0.2778, "w": 1.0, "h": 0.4444}


def test_crop_box_rounds_and_clamps() -> None:
    assert crop_box(1800, 1200, {"x": 0, "y": 0, "w": 1, "h": 1}) == (0, 0, 1800, 1200)
    assert crop_box(1200, 1800, PORTRAIT_FILL) == (0, 500, 1200, 1300)
    # rounding: 0.33335 * 1800 = 600.03 -> 600; (0.33335 + 0.5) * 1800 = 1500.03 -> 1500
    box = crop_box(1800, 1200, {"x": 0.33335, "y": 0.1, "w": 0.5, "h": 0.5})
    assert box == (600, 120, 1500, 720)
    # clamped inside the image, never empty
    assert crop_box(1800, 1200, {"x": -0.5, "y": -0.5, "w": 3, "h": 3}) == (0, 0, 1800, 1200)
    assert crop_box(1800, 1200, {"x": 0.9999, "y": 0.9999, "w": 0.0001, "h": 0.0001}) == (
        1799,
        1199,
        1800,
        1200,
    )
    assert crop_box(10, 10, {"x": 0.5, "y": 0.5, "w": 0.0, "h": 0.0}) == (5, 5, 6, 6)


def test_validate_crop_normalises() -> None:
    assert validate_crop(PORTRAIT_FILL, 1200, 1800) == PORTRAIT_FILL
    # ints are fine, the result holds floats rounded to four decimals
    out = validate_crop({"x": 0, "y": 0, "w": 1, "h": 1}, 1800, 1200)
    assert out == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
    assert all(isinstance(v, float) for v in out.values())
    assert validate_crop({"x": 0.123456, "y": 0.1, "w": 0.5, "h": 0.5}, 1800, 1200)["x"] == 0.1235
    # the aspect is judged in master pixels: a 2:3 master's full-width band
    # is 1.0 x 0.4444 in fractions, 3:2 in pixels
    assert validate_crop({"x": 0, "y": 0.1, "w": 1, "h": 0.4444}, 1200, 1800)["h"] == 0.4444
    # the tolerance: 1 percent either way (a square master: h = w / 1.5 is exact)
    validate_crop({"x": 0, "y": 0, "w": 0.6, "h": 0.6 / (1.5 * 1.008)}, 1200, 1200)
    validate_crop({"x": 0, "y": 0, "w": 0.6, "h": 0.6 / (1.5 * 0.992)}, 1200, 1200)
    # the minimum: 150 x 100 master pixels
    validate_crop({"x": 0, "y": 0, "w": 150 / 1800, "h": 100 / 1200}, 1800, 1200)


@pytest.mark.parametrize(
    ("crop", "fragment"),
    [
        ([0, 0, 1, 1], "crop must be an object"),
        (None, "crop must be an object"),
        ({"x": 0, "y": 0, "w": 1}, "crop.h is required"),
        ({"x": 0, "y": 0, "w": 1, "h": 1, "rotate": 90}, "crop has unknown key(s): rotate"),
        ({"x": "0", "y": 0, "w": 1, "h": 1}, "crop.x must be a finite number"),
        ({"x": 0, "y": True, "w": 1, "h": 1}, "crop.y must be a finite number"),
        ({"x": 0, "y": 0, "w": float("nan"), "h": 1}, "crop.w must be a finite number"),
        ({"x": 0, "y": 0, "w": 1, "h": float("inf")}, "crop.h must be a finite number"),
        ({"x": -0.01, "y": 0, "w": 1, "h": 1}, "crop.x must be at least 0"),
        ({"x": 0, "y": -1, "w": 1, "h": 1}, "crop.y must be at least 0"),
        ({"x": 0, "y": 0, "w": 0, "h": 1}, "crop.w must be greater than 0"),
        ({"x": 0, "y": 0, "w": 1, "h": -0.5}, "crop.h must be greater than 0"),
        ({"x": 0.5, "y": 0, "w": 0.6, "h": 0.6}, "crop.x + crop.w must be at most 1"),
        ({"x": 0, "y": 0.5, "w": 0.75, "h": 0.75}, "crop.y + crop.h must be at most 1"),
        ({"x": 0, "y": 0, "w": 1, "h": 0.5}, "crop: aspect must be 3:2"),
        ({"x": 0, "y": 0, "w": 0.6, "h": 0.6 / 1.02}, "crop: aspect must be 3:2"),
        ({"x": 0, "y": 0, "w": 0.05, "h": 0.05}, "crop: at least 150 x 100 master pixels"),
    ],
)
def test_validate_crop_errors(crop: object, fragment: str) -> None:
    with pytest.raises(ValueError, match=re.escape(fragment)):
        validate_crop(crop, 1800, 1200)


def test_cropped_fit_is_the_region_at_exactly_600_x_400() -> None:
    master = quadrants(1200, 1800)
    fit = fit_image(master, PORTRAIT_FILL)
    assert fit.box == FULL_BOX and fit.image.size == (600, 400) and fit.image.mode == "RGB"
    a = np.asarray(fit.image)
    # the band y 500..1300 of the master: red/green above its middle, blue/yellow below
    assert tuple(a[10, 10]) == RED_Q and tuple(a[10, 589]) == GREEN_Q
    assert tuple(a[389, 10]) == BLUE_Q and tuple(a[389, 589]) == YELLOW_Q
    assert not np.any(np.all(a == WHITE_RGB, axis=-1))  # no bars anywhere
    expected = master.crop((0, 500, 1200, 1300)).resize((600, 400), Image.Resampling.LANCZOS)
    assert np.array_equal(a, np.asarray(expected))
    assert np.array_equal(np.asarray(fit_master(master, PORTRAIT_FILL)), a)
    # a corner of a landscape master
    corner = fit_image(quadrants(1800, 1200), {"x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5})
    assert np.all(np.asarray(corner.image) == YELLOW_Q)
    # a region that is already 600 x 400 is taken as it is
    exact = fit_image(quadrants(1800, 1200), {"x": 0, "y": 0, "w": 600 / 1800, "h": 400 / 1200})
    assert np.array_equal(np.asarray(exact.image), np.asarray(quadrants(1800, 1200))[:400, :600])
    # the crop converts other modes too
    grey = fit_image(Image.new("L", (1800, 1200), 77), {"x": 0, "y": 0, "w": 1, "h": 1})
    assert grey.image.mode == "RGB" and grey.image.getpixel((10, 10)) == (77, 77, 77)


# The uncropped output must stay what it was before crops existed (the Pi has
# a cache full of renders under these keys).  The keys are platform
# independent; the pixel hashes are of Pillow's Lanczos resample on x86-64,
# recorded on 2026-11-09 before `fit_image` gained its `crop` parameter.
UNCROPPED_KEYS = {
    "default": "219e0d780bf47c07",
    "nominal": "c4cf85ddbf37baca",
    "stucki_nominal": "bd5a847e62797546",
}
UNCROPPED_FIT_SHA256 = {
    (1800, 1200): "5249c3a420402fd745eadda2fcf663f44a04a6986ba809cdf2a3dc6a8bab129a",
    (1600, 1200): "b97af9a5c7d44b4e2b06a682457364e92a5460c05388502376b5813438e11981",
    (800, 1200): "162fc2b1a9c2bfd69183d408238dd617021e2a0ee23d50d041a0effe6256e1f0",
    (1920, 1080): "c500f0f8f68fd90434aa071d654d591bf87506a3f1159ecd6c49a9273d64eaac",
}
UNCROPPED_FRAME_SHA256 = {
    (1800, 1200): "c11ee4d3d184c605b6fdc1ff28bb7515bf441c69ab34c31c075c6e69721f5b9f",
    (1600, 1200): "7a5e93798049998e5dcc1e4998364c93161b4c1ca974b7c2f7fb659ce7d6aba6",
    (800, 1200): "2a51399d75c8b05044151fd4481cb8fbf1fed30399c3c42a0aee21ebb9b0f4db",
    (1920, 1080): "b9fcd1af588c6dd26b630145fce7ba3668f245a34d52abec55e824a92d2ebbf2",
}


def test_uncropped_keys_are_unchanged() -> None:
    settings = Settings()
    assert pipeline.key_for(RECIPE, settings) == UNCROPPED_KEYS["default"]
    assert pipeline.key_for(RECIPE, settings, None) == UNCROPPED_KEYS["default"]
    assert pipeline.key_for({**RECIPE, "palette": "nominal"}, settings) == UNCROPPED_KEYS["nominal"]
    assert (
        recipe_key({**RECIPE, "method": "stucki"}, NOMINAL, settings.measured_palette())
        == UNCROPPED_KEYS["stucki_nominal"]
    )


@pytest.mark.skipif(
    platform.machine().lower() not in {"amd64", "x86_64"},
    reason="the recorded hashes are of Pillow's resample on x86-64",
)
@pytest.mark.parametrize("size", sorted(UNCROPPED_FIT_SHA256))
def test_uncropped_output_is_byte_identical(size: tuple[int, int]) -> None:
    master = gradient_image(*size)
    fit = fit_image(master)
    assert hashlib.sha256(np.asarray(fit.image).tobytes()).hexdigest() == UNCROPPED_FIT_SHA256[size]
    frame = render_frame(fit, DEFAULT_RECIPE, NOMINAL)
    assert hashlib.sha256(frame.tobytes()).hexdigest() == UNCROPPED_FRAME_SHA256[size]
    # and the default (no crop) is the same code path as before
    assert np.array_equal(np.asarray(fit_image(master, None).image), np.asarray(fit.image))


def test_crop_changes_the_key_only_when_given() -> None:
    base = recipe_key(RECIPE, MEASURED_DEFAULT, MEASURED_DEFAULT)
    assert recipe_key(RECIPE, MEASURED_DEFAULT, MEASURED_DEFAULT, crop=None) == base
    one = recipe_key(RECIPE, MEASURED_DEFAULT, MEASURED_DEFAULT, crop=PORTRAIT_FILL)
    two = recipe_key(RECIPE, MEASURED_DEFAULT, MEASURED_DEFAULT, crop={**PORTRAIT_FILL, "y": 0.1})
    assert valid_key(one) and one != base and two != base and one != two
    # the same crop however it is spelt: ints, key order
    spelt = {"h": 0.4444, "w": 1, "y": 0.2778, "x": 0}
    assert recipe_key(RECIPE, MEASURED_DEFAULT, MEASURED_DEFAULT, crop=spelt) == one
    settings = Settings()
    assert pipeline.key_for(RECIPE, settings, PORTRAIT_FILL) != pipeline.key_for(RECIPE, settings)
    assert pipeline.key_for(RECIPE, settings, PORTRAIT_FILL) == recipe_key(
        RECIPE, settings.measured_palette(), settings.measured_palette(), crop=PORTRAIT_FILL
    )


def test_load_fit_distinguishes_crops(photo_factory: PhotoFactory, data_paths: DataPaths) -> None:
    photo = photo_factory(quadrants(1200, 1800))
    plain = pipeline.load_fit(photo, data_paths)
    assert plain.box == (166, 0, 433, 400)
    cropped_photo = dataclasses.replace(photo, crop=PORTRAIT_FILL)
    cropped = pipeline.load_fit(cropped_photo, data_paths)
    assert cropped is not plain and cropped.box == FULL_BOX
    master = Image.open(pipeline.master_file(photo, data_paths))
    expected = fit_image(master, PORTRAIT_FILL).image
    assert np.array_equal(np.asarray(cropped.image), np.asarray(expected))
    # both stay memoised side by side, and the same crop spelt differently hits
    assert pipeline.load_fit(photo, data_paths) is plain
    assert pipeline.load_fit(cropped_photo, data_paths) is cropped
    spelt = dataclasses.replace(photo, crop={"h": 0.4444, "w": 1.0, "y": 0.2778, "x": 0.0})
    assert pipeline.load_fit(spelt, data_paths) is cropped
    assert len(pipeline._FITS) == 2
    higher = dataclasses.replace(photo, crop={**PORTRAIT_FILL, "y": 0.0})
    other = pipeline.load_fit(higher, data_paths)
    assert other is not cropped and len(pipeline._FITS) == 3
    pipeline.forget_fit(photo.id)
    assert len(pipeline._FITS) == 0


# --- render --------------------------------------------------------------------------------


def test_render_frame_is_adjust_then_dither_at_one_to_one() -> None:
    fit = fit_image(gradient_image(1800, 1200))
    recipe = {**RECIPE, "brightness": 1.1, "gamma": 1.3}
    frame = render_frame(fit, recipe, MEASURED_DEFAULT)
    assert frame.shape == (PANEL_HEIGHT, PANEL_WIDTH) and frame.dtype == np.uint8
    assert frame.max() <= 5
    manual = dither(
        np.asarray(adjust.apply_recipe(fit.image, recipe)),
        MEASURED_DEFAULT,
        "atkinson",
        serpentine=True,
        strength=0.85,
        distance="lab",
    )
    assert np.array_equal(frame, manual)
    # a plain 600x400 image is the same as a full-frame Fit
    assert np.array_equal(render_frame(fit.image, recipe, MEASURED_DEFAULT), manual)
    assert len(pack_frame(frame)) == 120000


@pytest.mark.parametrize("method", METHOD_IDS)
def test_bars_stay_palette_white_whatever_the_recipe(method: str) -> None:
    fit = fit_image(gradient_image(800, 1200))
    harsh = {**RECIPE, "method": method, "brightness": 0.5, "contrast": 1.5, "sharpen": 2.0}
    frame = render_frame(fit, harsh, MEASURED_DEFAULT)
    x0, y0, x1, y1 = fit.box
    assert np.all(frame[:, :x0] == WHITE) and np.all(frame[:, x1:] == WHITE)
    inside = frame[y0:y1, x0:x1]
    assert len(np.unique(inside)) > 1
    content = np.asarray(adjust.apply_recipe(fit.image.crop(fit.box), harsh))
    expected = dither(content, MEASURED_DEFAULT, method, serpentine=True, strength=0.85)
    assert np.array_equal(inside, expected)


def test_render_frame_validates() -> None:
    fit = fit_image(Image.new("RGB", (60, 40)))
    with pytest.raises(RecipeError, match="strength"):
        render_frame(fit, {**RECIPE, "strength": 2.0}, NOMINAL)
    with pytest.raises(RecipeError, match="method"):
        render_frame(fit, {**RECIPE, "method": "crosshatch"}, NOMINAL)
    with pytest.raises(ValueError, match="600x400"):
        render_frame(Image.new("RGB", (60, 40)), RECIPE, NOMINAL)


# --- recipes and keys ------------------------------------------------------------------------


def test_complete_recipe_normalises_and_merges() -> None:
    r = complete_recipe({"brightness": 1}, base=RECIPE)
    assert r == {**RECIPE, "brightness": 1.0} and isinstance(r["brightness"], float)
    assert list(r) == list(DEFAULT_RECIPE)
    with pytest.raises(RecipeError, match="missing"):
        complete_recipe({"method": "atkinson"})
    with pytest.raises(RecipeError, match="unknown key"):
        complete_recipe({"brightnes": 1.0}, base=RECIPE)
    with pytest.raises(RecipeError, match="object"):
        complete_recipe([1, 2])
    with pytest.raises(RecipeError, match="crosshatch"):
        complete_recipe({"method": "crosshatch"}, base=RECIPE)


@pytest.mark.parametrize("knob", ["strength", "brightness", "gamma", "sharpen"])
def test_huge_integers_are_recipe_errors(knob: str) -> None:
    # valid JSON, but float() of it raises OverflowError: still a RecipeError
    with pytest.raises(RecipeError, match=knob):
        complete_recipe({knob: 10**400}, base=RECIPE)
    with pytest.raises(RecipeError, match=knob):
        complete_recipe({knob: -(10**400)}, base=RECIPE)


def test_recipe_key_is_stable_and_order_independent() -> None:
    key = recipe_key(RECIPE, MEASURED_DEFAULT, MEASURED_DEFAULT)
    assert valid_key(key) and len(key) == 16
    assert recipe_key(dict(RECIPE), MEASURED_DEFAULT, MEASURED_DEFAULT) == key
    reordered = dict(reversed(list(RECIPE.items())))
    assert recipe_key(reordered, MEASURED_DEFAULT, MEASURED_DEFAULT) == key
    as_ints = {**RECIPE, "brightness": 1, "contrast": 1, "gamma": 1, "sharpen": 0}
    assert recipe_key(as_ints, MEASURED_DEFAULT, MEASURED_DEFAULT) == key


ALTERNATIVES = {
    "method": "floyd_steinberg",
    "serpentine": False,
    "strength": 0.9,
    "distance": "rgb",
    "brightness": 1.05,
    "contrast": 0.95,
    "saturation": 1.0,
    "gamma": 1.2,
    "sharpen": 0.5,
    "palette": "nominal",
}


@pytest.mark.parametrize("knob", list(DEFAULT_RECIPE))
def test_every_knob_changes_the_key(knob: str) -> None:
    assert ALTERNATIVES[knob] != RECIPE[knob]
    base = recipe_key(RECIPE, MEASURED_DEFAULT, MEASURED_DEFAULT)
    assert (
        recipe_key({**RECIPE, knob: ALTERNATIVES[knob]}, MEASURED_DEFAULT, MEASURED_DEFAULT) != base
    )


def test_palettes_and_format_version_change_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    base = recipe_key(RECIPE, MEASURED_DEFAULT, MEASURED_DEFAULT)
    tweaked = MEASURED_DEFAULT.with_color(4, (40, 60, 140))
    assert recipe_key(RECIPE, NOMINAL, MEASURED_DEFAULT) != base
    assert recipe_key(RECIPE, tweaked, MEASURED_DEFAULT) != base
    assert recipe_key(RECIPE, MEASURED_DEFAULT, tweaked) != base
    assert recipe_key(RECIPE, Palette("other", MEASURED_DEFAULT.rgb), MEASURED_DEFAULT) == base
    monkeypatch.setattr(pipeline, "FORMAT_VERSION", pipeline.FORMAT_VERSION + 1)
    assert recipe_key(RECIPE, MEASURED_DEFAULT, MEASURED_DEFAULT) != base


def test_engine_output_changes_the_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # not just the ctypes ABI: the engine's source and blue-noise texture too
    version = pipeline.engine_version()
    assert version.startswith(f"{pipeline.dither_engine.ABI_VERSION}:") and len(version) > 4
    assert pipeline.engine_version() is version  # computed once
    base = recipe_key(RECIPE, MEASURED_DEFAULT, MEASURED_DEFAULT)
    other_noise = tmp_path / "blue_noise_64.npy"
    other_noise.write_bytes(pipeline.dither_engine.BLUE_NOISE_PATH.read_bytes() + b"\x00")
    monkeypatch.setattr(pipeline.dither_engine, "BLUE_NOISE_PATH", other_noise)
    changed = pipeline.engine_version.__wrapped__()
    assert changed != version and changed.split(":")[0] == version.split(":")[0]
    monkeypatch.setattr(pipeline, "engine_version", lambda: changed)
    assert recipe_key(RECIPE, MEASURED_DEFAULT, MEASURED_DEFAULT) != base


def _row(database: Database, recipe: dict | None) -> Photo:
    """A photo row with a given stored recipe (no master needed)."""
    return database.add_photo(
        original_name="x", width=60, height=40, master_path="m", master_bytes=1, recipe=recipe
    )


def test_effective_recipe_and_palette(database: Database, photo_factory: PhotoFactory) -> None:
    settings = Settings()
    plain = photo_factory(Image.new("RGB", (60, 40)))
    assert effective_recipe(plain, settings) == RECIPE
    custom = settings.merged({"default_recipe": {"method": "stucki", "strength": 0.7}})
    assert effective_recipe(plain, custom)["method"] == "stucki"
    over = {**RECIPE, "method": "bayer_8"}
    assert effective_recipe(_row(database, over), custom) == over
    # a stored partial recipe (from an older version) merges over the default
    partial = _row(database, {"method": "burkes"})
    assert effective_recipe(partial, custom) == {**custom.default_recipe, "method": "burkes"}
    # a stored method the registry no longer has falls back to the default's
    # method alone; the override's other knobs still apply
    broken = _row(database, {**RECIPE, "method": "gone_method", "gamma": 1.7})
    assert effective_recipe(broken, custom) == {**RECIPE, "method": "stucki", "gamma": 1.7}
    narrowed = _row(database, {"gamma": 9.0, "sharpen": 0.5, "retired_knob": 1})
    assert effective_recipe(narrowed, custom) == {**custom.default_recipe, "sharpen": 0.5}
    odd_default = settings.merged({"default_recipe": {"method": "not_a_method"}})
    assert effective_recipe(plain, odd_default) == RECIPE  # falls back to the built-in

    tuned = settings.merged({"palette": {"measured": {"blue": [1, 2, 3]}}})
    assert resolve_render_palette(RECIPE, tuned) == tuned.measured_palette()
    assert resolve_render_palette({**RECIPE, "palette": "nominal"}, tuned) is NOMINAL
    assert pipeline.key_for(RECIPE, tuned) != pipeline.key_for(RECIPE, settings)


def test_use_measured_off_dithers_against_nominal(
    photo_factory: PhotoFactory, data_paths: DataPaths
) -> None:
    """``settings.palette.use_measured`` is the global switch: off, a recipe
    asking for "measured" dithers against the nominal primaries (the key
    follows, since it covers the palette dithered against); a "nominal"
    recipe is unaffected; the PNG is still coloured with the measured one."""
    on = Settings().merged({"palette": {"measured": {"blue": [1, 2, 3]}}})
    off = on.merged({"palette": {"use_measured": False}})
    nominal_recipe = {**RECIPE, "palette": "nominal"}
    assert RECIPE["palette"] == "measured" and on.palette.use_measured
    assert resolve_render_palette(RECIPE, on) == on.measured_palette()
    assert resolve_render_palette(RECIPE, off) is NOMINAL
    assert resolve_render_palette(nominal_recipe, off) is NOMINAL
    assert off.render_palette() is NOMINAL  # what the apps dither against agrees
    assert pipeline.key_for(RECIPE, off) != pipeline.key_for(RECIPE, on)
    assert pipeline.key_for(nominal_recipe, off) == pipeline.key_for(nominal_recipe, on)
    assert pipeline.key_for(RECIPE, off) == recipe_key(RECIPE, NOMINAL, on.measured_palette())

    photo = photo_factory()
    cache = RenderCache(data_paths)
    fit = pipeline.load_fit(photo, data_paths)
    measured = cache.render_or_get(photo, None, on)
    nominal = cache.render_or_get(photo, None, off)
    assert nominal.recipe == measured.recipe == RECIPE
    assert not nominal.cached and nominal.key == pipeline.key_for(RECIPE, off) != measured.key
    assert np.array_equal(nominal.frame, render_frame(fit, RECIPE, NOMINAL))
    assert not np.array_equal(nominal.frame, measured.frame)
    data = cache.read_png(photo.id, nominal.key)
    assert data is not None
    with Image.open(io.BytesIO(data)) as im:
        assert im.getpalette()[:18] == on.measured_palette().flat()
    # the editor's method strip follows the switch too
    (strip,) = cache.render_methods(photo, RECIPE, off, methods=(METHODS_BY_ID[RECIPE["method"]],))
    assert strip.method.id == RECIPE["method"] and strip.key == nominal.key and strip.cached


def test_unusable_default_method_falls_back_alone(
    database: Database, photo_factory: PhotoFactory, caplog: pytest.LogCaptureFixture
) -> None:
    # settings.json is validated without the dither registry, so PUT
    # /api/settings can store a method this build lacks: only that knob
    # falls back to the built-in default, the user's other knobs still apply
    stored = {"method": "gone_method", "gamma": 1.3, "palette": "nominal", "strength": 0.6}
    settings = Settings().merged({"default_recipe": stored})
    salvaged = {**RECIPE, "gamma": 1.3, "palette": "nominal", "strength": 0.6}
    with caplog.at_level("WARNING", logger="inkframe.render.pipeline"):
        assert pipeline.default_recipe(settings) == salvaged
        assert pipeline.default_recipe(settings) == salvaged
    assert len([r for r in caplog.records if "gone_method" in r.getMessage()]) <= 1
    plain = photo_factory(Image.new("RGB", (60, 40)))
    assert effective_recipe(plain, settings) == salvaged
    own = _row(database, {"method": "sierra"})  # an override merges over the salvaged default
    assert effective_recipe(own, settings) == {**salvaged, "method": "sierra"}


# --- the fit LRU -----------------------------------------------------------------------------


def test_load_fit_is_memoised_and_tracks_the_file(
    photo_factory: PhotoFactory, data_paths: DataPaths
) -> None:
    photo = photo_factory()
    first = pipeline.load_fit(photo, data_paths)
    assert pipeline.load_fit(photo, data_paths) is first
    assert first.box == FULL_BOX
    master = pipeline.master_file(photo, data_paths)
    assert master == data_paths.root / photo.master_path
    Image.new("RGB", (900, 1200), (5, 5, 5)).save(master, "JPEG")
    st = master.stat()
    os.utime(master, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    second = pipeline.load_fit(photo, data_paths)
    assert second is not first and second.box == (150, 0, 450, 400)
    pipeline.forget_fit(photo.id)
    assert pipeline.load_fit(photo, data_paths) is not second


def test_load_fit_lru_evicts(photo_factory: PhotoFactory, data_paths: DataPaths) -> None:
    photos = [photo_factory(Image.new("RGB", (60, 40), (i, i, i))) for i in range(6)]
    fits = [pipeline.load_fit(p, data_paths) for p in photos]
    assert len(pipeline._FITS) == pipeline.FIT_CACHE_SIZE == 4
    assert pipeline.load_fit(photos[-1], data_paths) is fits[-1]
    assert pipeline.load_fit(photos[0], data_paths) is not fits[0]


def test_load_fit_missing_or_corrupt_master(
    photo_factory: PhotoFactory, data_paths: DataPaths
) -> None:
    photo = photo_factory(Image.new("RGB", (60, 40)))
    path = pipeline.master_file(photo, data_paths)
    path.write_bytes(b"not a jpeg")
    with pytest.raises(pipeline.MasterMissing, match="unreadable"):
        pipeline.load_fit(photo, data_paths)
    path.unlink()
    with pytest.raises(pipeline.MasterMissing, match="missing"):
        pipeline.load_fit(photo, data_paths)


@pytest.mark.parametrize(
    "stored", ["../inkframe.db", "masters/../settings.json", "renders/x.jpg", "m", "ABSOLUTE"]
)
def test_master_paths_outside_masters_are_refused(
    database: Database, data_paths: DataPaths, tmp_path: Path, stored: str
) -> None:
    # defence in depth: a database value never makes the pipeline read (or a
    # delete remove) a file outside <data>/masters
    outside = tmp_path / "elsewhere.jpg"
    Image.new("RGB", (60, 40)).save(outside, "JPEG")
    value = str(outside) if stored == "ABSOLUTE" else stored
    photo = database.add_photo(
        original_name="x", width=60, height=40, master_path=value, master_bytes=1
    )
    with pytest.raises(pipeline.MasterMissing, match="outside"):
        pipeline.master_file(photo, data_paths)
    with pytest.raises(pipeline.MasterMissing):
        pipeline.load_fit(photo, data_paths)
    assert outside.is_file()
    inside = database.add_photo(
        original_name="y", width=60, height=40, master_path="masters/abc.jpg", master_bytes=1
    )
    assert pipeline.master_file(inside, data_paths) == data_paths.masters / "abc.jpg"


# --- the render cache --------------------------------------------------------------------------

PID = "0123456789abcdef0123456789abcdef"
KEY = "0123456789abcdef"


def _random_frame(seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 6, (400, 600), dtype=np.uint8)


def test_put_get_round_trip_is_exact(data_paths: DataPaths) -> None:
    cache = RenderCache(data_paths)
    frame = _random_frame()
    path = cache.put(PID, KEY, frame, MEASURED_DEFAULT)
    assert path == data_paths.renders / PID / f"{KEY}.png" == cache.path(PID, KEY)
    assert cache.has(PID, KEY)
    assert np.array_equal(cache.get(PID, KEY), frame)
    data = path.read_bytes()
    assert cache.read_png(PID, KEY) == data
    with Image.open(io.BytesIO(data)) as im:
        assert im.format == "PNG" and im.mode == "P" and im.size == (600, 400)
        assert im.getpalette()[:18] == MEASURED_DEFAULT.flat()
        assert len(im.getpalette()) == 18  # exactly six colours
    assert data[24] == 4  # IHDR bit depth: a 4-bit paletted PNG
    assert [p.name for p in path.parent.iterdir()] == [f"{KEY}.png"]  # no temp files


def test_png_colours_are_informational(data_paths: DataPaths) -> None:
    cache = RenderCache(data_paths)
    frame = _random_frame(1)
    cache.put(PID, KEY, frame, NOMINAL)
    other = "f" * 16
    cache.put(PID, other, frame, MEASURED_DEFAULT)
    assert np.array_equal(cache.get(PID, KEY), cache.get(PID, other))
    assert cache.read_png(PID, KEY) != cache.read_png(PID, other)


def test_misses_and_corrupt_files(data_paths: DataPaths) -> None:
    cache = RenderCache(data_paths)
    assert cache.get(PID, KEY) is None and cache.read_png(PID, KEY) is None
    assert not cache.has(PID, KEY) and cache.keys(PID) == []
    cache.path(PID, KEY).parent.mkdir(parents=True)
    cache.path(PID, KEY).write_bytes(b"\x89PNG\r\n\x1a\ngarbage")
    assert cache.get(PID, KEY) is None
    assert not cache.path(PID, KEY).exists()  # a corrupt render is removed
    wrong_shape = Image.fromarray(np.zeros((40, 60), np.uint8))
    wrong_shape.putpalette(NOMINAL.flat())
    wrong_shape.save(cache.path(PID, KEY), "PNG")
    assert cache.get(PID, KEY) is None
    bad_index = Image.fromarray(np.full((400, 600), 7, np.uint8))
    bad_index.putpalette(NOMINAL.flat() + [9] * 6)
    assert decode_png(_png(bad_index)) is None
    assert decode_png(_png(Image.new("RGB", (600, 400)))) is None
    assert decode_png(b"GIF89a") is None


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.parametrize(
    "content",
    [
        b"\x89PNG\r\n\x1a\ngarbage",
        _png(Image.new("RGB", (600, 400), (1, 2, 3))),  # a PNG, but not a frame
        _png(Image.new("P", (60, 40))),  # wrong size
    ],
)
def test_read_png_serves_only_valid_frames(data_paths: DataPaths, content: bytes) -> None:
    cache = RenderCache(data_paths)
    good = cache.put(PID, KEY, _random_frame(), MEASURED_DEFAULT)
    assert cache.read_png(PID, KEY) == good.read_bytes()
    good.write_bytes(content)
    assert cache.read_png(PID, KEY) is None
    assert not good.exists()  # removed, like a corrupt file found by get()


def test_encode_png_validates_the_frame() -> None:
    with pytest.raises(ValueError):
        encode_png(np.zeros((10, 10), np.uint8), NOMINAL)
    with pytest.raises(ValueError):
        encode_png(np.full((400, 600), 6, np.uint8), NOMINAL)


def test_paths_reject_malformed_ids(data_paths: DataPaths) -> None:
    cache = RenderCache(data_paths)
    for bad_id in ("../x", "ABCDEF0123456789ABCDEF0123456789", "1" * 31, ""):
        with pytest.raises(ValueError):
            cache.dir(bad_id)
    for bad_key in ("../../etc", "0123456789ABCDEF", "1" * 15, "x" * 16):
        with pytest.raises(ValueError):
            cache.path(PID, bad_key)


def test_invalidate(data_paths: DataPaths) -> None:
    cache = RenderCache(data_paths)
    cache.invalidate(PID)  # nothing there yet: fine
    cache.put(PID, KEY, _random_frame(), NOMINAL)
    cache.invalidate(PID)
    assert not cache.dir(PID).exists() and cache.get(PID, KEY) is None


def test_prune_drops_oldest_beyond_the_limit(data_paths: DataPaths) -> None:
    cache = RenderCache(data_paths)
    frame = _random_frame()
    keys = [f"{i:016x}" for i in range(MAX_RENDERS_PER_PHOTO + 5)]
    now = time.time()
    for i, key in enumerate(keys):
        path = cache.put(PID, key, frame, NOMINAL)
        os.utime(path, (now - 1000 + i, now - 1000 + i))
    stale_tmp = cache.dir(PID) / f".{keys[0]}.png.abc.tmp"
    stale_tmp.write_bytes(b"x")
    os.utime(stale_tmp, (now - 3600, now - 3600))
    fresh_tmp = cache.dir(PID) / f".{keys[1]}.png.def.tmp"
    fresh_tmp.write_bytes(b"x")

    assert cache.prune(PID, keep_keys={keys[0], keys[2]}) == 5
    left = set(cache.keys(PID))
    assert len(left) == MAX_RENDERS_PER_PHOTO
    assert keys[0] in left and keys[2] in left  # kept although oldest
    assert not left & {keys[1], keys[3], keys[4], keys[5], keys[6]}
    assert not stale_tmp.exists() and fresh_tmp.exists()
    assert cache.prune(PID) == 0  # at the limit: nothing to do
    assert cache.prune("f" * 32) == 0  # no directory: fine


def test_render_or_get(photo_factory: PhotoFactory, data_paths: DataPaths) -> None:
    settings = Settings()
    photo = photo_factory()
    cache = RenderCache(data_paths)
    first = cache.render_or_get(photo, None, settings)
    assert not first.cached and first.recipe == RECIPE
    assert first.key == pipeline.key_for(RECIPE, settings)
    fit = pipeline.load_fit(photo, data_paths)
    assert np.array_equal(first.frame, render_frame(fit, RECIPE, settings.measured_palette()))
    second = cache.render_or_get(photo, None, settings)
    assert second.cached and np.array_equal(second.frame, first.frame)
    nominal = cache.render_or_get(photo, {**RECIPE, "palette": "nominal"}, settings)
    assert nominal.key != first.key
    assert np.array_equal(nominal.frame, render_frame(fit, RECIPE, NOMINAL))
    loader_calls = []

    def loader(p):
        loader_calls.append(p.id)
        return pipeline.fit_image(Image.new("RGB", (60, 40), (250, 0, 0)))

    red = cache.render_or_get(photo, {**RECIPE, "method": "none"}, settings, loader)
    assert loader_calls == [photo.id]
    assert np.all(red.frame == 3)  # every pixel the measured red


def test_render_methods_reuses_one_fit(photo_factory: PhotoFactory, data_paths: DataPaths) -> None:
    settings = Settings()
    photo = photo_factory()
    cache = RenderCache(data_paths)
    calls = []

    def loader(p):
        calls.append(p.id)
        return pipeline.load_fit(p, data_paths)

    renders = cache.render_methods(photo, RECIPE, settings, master_loader=loader)
    assert [r.method.id for r in renders] == list(METHOD_IDS) and calls == [photo.id]
    assert len({r.key for r in renders}) == len(METHODS)
    assert not any(r.cached for r in renders)
    fit = pipeline.load_fit(photo, data_paths)
    for r in renders:
        expected = render_frame(fit, {**RECIPE, "method": r.method.id}, settings.measured_palette())
        assert np.array_equal(cache.get(photo.id, r.key), expected), r.method.id
    again = cache.render_methods(photo, RECIPE, settings, master_loader=loader)
    assert all(r.cached for r in again) and calls == [photo.id]
    assert [r.key for r in again] == [r.key for r in renders]
    assert renders[2].key == cache.render_or_get(photo, None, settings).key  # atkinson


def test_render_methods_rerenders_a_corrupt_cached_preview(
    photo_factory: PhotoFactory, data_paths: DataPaths
) -> None:
    # a preview key is not the photo's current one, so its URL cannot render
    # it on a miss: the set must not report a file it cannot serve as cached
    settings = Settings()
    photo = photo_factory(Image.new("RGB", (60, 40), (10, 200, 30)))
    cache = RenderCache(data_paths)
    renders = cache.render_methods(photo, RECIPE, settings)
    bayer = next(r for r in renders if r.method.id == "bayer_4")
    cache.path(photo.id, bayer.key).write_bytes(cache_module.PNG_MAGIC + b"not a frame")
    again = {r.method.id: r for r in cache.render_methods(photo, RECIPE, settings)}
    assert not again["bayer_4"].cached and again["bayer_4"].key == bayer.key
    assert all(r.cached for m, r in again.items() if m != "bayer_4")
    assert cache.read_png(photo.id, bayer.key) is not None


def test_cache_files_live_under_the_photo(
    photo_factory: PhotoFactory, data_paths: DataPaths
) -> None:
    photo = photo_factory(Image.new("RGB", (60, 40)))
    cache = RenderCache(data_paths)
    rendered = cache.render_or_get(photo, None, Settings())
    assert sorted(Path(data_paths.renders).rglob("*.png")) == [
        data_paths.renders / photo.id / f"{rendered.key}.png"
    ]
