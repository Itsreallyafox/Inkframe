"""The render cache and crops: ``render_or_get`` and ``render_methods`` key
and render by ``photo.crop``, or by a crop override (the preview of a crop
that is not saved yet).  The rest of the cache is covered in
``test_pipeline.py``."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from PIL import Image

from inkframe.config import DEFAULT_RECIPE, Settings
from inkframe.db import Photo
from inkframe.display.palette import WHITE
from inkframe.paths import DataPaths
from inkframe.render import pipeline
from inkframe.render.cache import RenderCache
from inkframe.render.dither import METHODS_BY_ID
from tests.conftest import PhotoFactory
from tests.test_pipeline import PORTRAIT_FILL, quadrants

RECIPE = pipeline.complete_recipe(DEFAULT_RECIPE)
TOP = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 0.4444}


@pytest.fixture(autouse=True)
def _fresh_fit_cache() -> None:
    pipeline.clear_fit_cache()


def _master(photo: Photo, data_paths: DataPaths) -> Image.Image:
    """The stored master (a JPEG: not the exact pixels the factory was given)."""
    return Image.open(pipeline.master_file(photo, data_paths))


def test_render_or_get_keys_by_the_photo_crop(
    photo_factory: PhotoFactory, data_paths: DataPaths
) -> None:
    settings = Settings()
    photo = photo_factory(quadrants(1200, 1800))
    cache = RenderCache(data_paths)
    plain = cache.render_or_get(photo, None, settings)
    assert plain.key == pipeline.key_for(RECIPE, settings)
    assert np.all(plain.frame[:, :166] == WHITE)  # letterboxed

    cropped_photo = dataclasses.replace(photo, crop=PORTRAIT_FILL)
    cropped = cache.render_or_get(cropped_photo, None, settings)
    assert not cropped.cached
    assert cropped.key == pipeline.key_for(RECIPE, settings, PORTRAIT_FILL) != plain.key
    assert cache.has(photo.id, cropped.key) and cache.has(photo.id, plain.key)
    assert not np.array_equal(cropped.frame, plain.frame)
    expected = pipeline.render_frame(
        pipeline.fit_image(_master(photo, data_paths), PORTRAIT_FILL),
        RECIPE,
        settings.measured_palette(),
    )
    assert np.array_equal(cropped.frame, expected)
    assert (cropped.frame == WHITE).mean() < 0.02  # no bars: the band fills the panel
    # the second ask is a hit under the crop-aware key
    again = cache.render_or_get(cropped_photo, None, settings)
    assert again.cached and again.key == cropped.key
    # and a stale (uncropped) render is never what a cropped photo gets
    assert cache.get(photo.id, plain.key) is not None
    assert not np.array_equal(cache.get(photo.id, plain.key), again.frame)


def test_render_or_get_crop_override(photo_factory: PhotoFactory, data_paths: DataPaths) -> None:
    settings = Settings()
    photo = photo_factory(quadrants(1200, 1800))
    cache = RenderCache(data_paths)
    top = cache.render_or_get(photo, None, settings, crop=TOP)
    assert top.key == pipeline.key_for(RECIPE, settings, TOP)
    assert photo.crop is None  # the override stores nothing on the photo
    # the top band of the master is red (left) and green (right): no blue at all
    assert 4 not in set(np.unique(top.frame).tolist())
    # the override wins over the photo's own crop
    cropped_photo = dataclasses.replace(photo, crop=PORTRAIT_FILL)
    assert cache.render_or_get(cropped_photo, None, settings, crop=TOP).key == top.key
    # a master_loader is used as given, whatever the crop (its caller fitted it)
    calls = []

    def loader(p):
        calls.append(p.crop)
        return pipeline.fit_image(Image.new("RGB", (60, 40), (250, 0, 0)))

    red = cache.render_or_get(photo, {**RECIPE, "method": "none"}, settings, loader, crop=TOP)
    assert calls == [None] and np.all(red.frame == 3)


def test_render_methods_with_a_crop_override(
    photo_factory: PhotoFactory, data_paths: DataPaths
) -> None:
    settings = Settings()
    photo = photo_factory(quadrants(1200, 1800))
    cache = RenderCache(data_paths)
    methods = (METHODS_BY_ID["none"], METHODS_BY_ID["atkinson"])
    plain = cache.render_methods(photo, RECIPE, settings, methods=methods)
    draft = cache.render_methods(photo, RECIPE, settings, methods=methods, crop=TOP)
    assert [r.key for r in draft] != [r.key for r in plain]
    assert all(not r.cached for r in draft)
    for r in draft:
        assert r.key == pipeline.key_for({**RECIPE, "method": r.method.id}, settings, TOP)
        frame = cache.get(photo.id, r.key)
        assert frame is not None
        expected = pipeline.render_frame(
            pipeline.fit_image(_master(photo, data_paths), TOP),
            {**RECIPE, "method": r.method.id},
            settings.measured_palette(),
        )
        assert np.array_equal(frame, expected), r.method.id
    # asking again is all hits; saving that crop finds the same renders
    again = cache.render_methods(photo, RECIPE, settings, methods=methods, crop=TOP)
    assert all(r.cached for r in again) and [r.key for r in again] == [r.key for r in draft]
    saved = cache.render_or_get(dataclasses.replace(photo, crop=TOP), None, settings)
    assert saved.cached and saved.key == draft[1].key  # atkinson, the default method
    # a cropped photo previews with its own crop when no override is given
    cropped_photo = dataclasses.replace(photo, crop=TOP)
    own = cache.render_methods(cropped_photo, RECIPE, settings, methods=methods)
    assert [r.key for r in own] == [r.key for r in draft] and all(r.cached for r in own)
