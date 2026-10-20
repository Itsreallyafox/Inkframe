"""The Photos app: contract, cached photo frames, the no-photos card, failures."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from inkframe.apps import load_app
from inkframe.apps import photos as P
from inkframe.apps.base import App, AppError, FontLibrary, RenderContext, render_to_frame
from inkframe.config import DEFAULT_RECIPE, Settings
from inkframe.db import Database
from inkframe.display.palette import (
    BLACK,
    BLUE,
    GREEN,
    RED,
    WHITE,
    YELLOW,
    indices_to_image,
    pack_frame,
)
from inkframe.paths import DataPaths
from inkframe.render import pipeline
from inkframe.render.cache import RenderCache
from tests.conftest import PhotoFactory

NOW = datetime(2026, 10, 19, 9, 30, tzinfo=timezone.utc)
FONTS = FontLibrary()
RECIPE = pipeline.complete_recipe(DEFAULT_RECIPE)


@pytest.fixture(autouse=True)
def _fresh_fit_cache() -> None:
    pipeline.clear_fit_cache()


def _ctx(
    tmp_path: Path,
    db: Database | None = None,
    paths: DataPaths | None = None,
    item_id: str | None = None,
    settings: Settings | None = None,
) -> RenderContext:
    return RenderContext(
        settings={},
        now=NOW,
        cache_dir=tmp_path,
        fonts=FONTS,
        online=False,
        db=db,
        paths=paths,
        app_settings=settings if settings is not None else Settings(),
        item_id=item_id,
    )


def test_contract() -> None:
    app = P.APP
    assert isinstance(app, App) and load_app("photos") is app
    assert (app.id, app.name, app.description) == ("photos", "Photos", "Your uploaded photos")
    assert app.needs_dither is False and app.variants == 1 and app.settings_schema == ()


def test_photo_frame_comes_from_the_cache(
    tmp_path: Path, database: Database, data_paths: DataPaths, photo_factory: PhotoFactory
) -> None:
    photo = photo_factory()
    settings = Settings()
    frame = render_to_frame(P.APP, _ctx(tmp_path, database, data_paths, photo.id, settings))
    assert frame.shape == (400, 600) and frame.dtype == np.uint8
    assert len(pack_frame(frame)) == 120000
    key = pipeline.key_for(RECIPE, settings)
    cached = RenderCache(data_paths).get(photo.id, key)
    assert cached is not None and np.array_equal(frame, cached)
    fit = pipeline.load_fit(photo, data_paths)
    assert np.array_equal(frame, pipeline.render_frame(fit, RECIPE, settings.measured_palette()))


def test_cache_hit_does_not_dither(
    tmp_path: Path,
    database: Database,
    data_paths: DataPaths,
    photo_factory: PhotoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photo = photo_factory()
    first = P.APP.render(_ctx(tmp_path, database, data_paths, photo.id))

    def no_render(*_a: object, **_kw: object) -> None:
        raise AssertionError("a cached photo must not be rendered again")

    monkeypatch.setattr(pipeline, "render_frame", no_render)
    assert np.array_equal(P.APP.render(_ctx(tmp_path, database, data_paths, photo.id)), first)


def test_photo_recipe_and_settings_are_honoured(
    tmp_path: Path, database: Database, data_paths: DataPaths, photo_factory: PhotoFactory
) -> None:
    own = {**RECIPE, "method": "bayer_4", "palette": "nominal"}
    photo = photo_factory(recipe=own)
    frame = P.APP.render(_ctx(tmp_path, database, data_paths, photo.id))
    fit = pipeline.load_fit(photo, data_paths)
    assert np.array_equal(frame, pipeline.render_frame(fit, own, pipeline.NOMINAL))

    plain = photo_factory(Image.new("RGB", (900, 600), (40, 60, 140)))
    tuned = Settings().merged(
        {"default_recipe": {"method": "none"}, "palette": {"measured": {"blue": [40, 60, 140]}}}
    )
    frame = P.APP.render(_ctx(tmp_path, database, data_paths, plain.id, tuned))
    assert np.all(frame == BLUE)  # the exact measured blue maps to blue everywhere


def test_portrait_photo_has_white_bars(
    tmp_path: Path, database: Database, data_paths: DataPaths, photo_factory: PhotoFactory
) -> None:
    photo = photo_factory(Image.new("RGB", (800, 1200), (20, 20, 20)))
    frame = P.APP.render(_ctx(tmp_path, database, data_paths, photo.id))
    assert np.all(frame[:, :166] == WHITE) and np.all(frame[:, 433:] == WHITE)
    assert np.all(frame[:, 170:430] == BLACK)


def test_no_photos_card(tmp_path: Path, database: Database, data_paths: DataPaths) -> None:
    expected = P.no_photos_card(FONTS).to_frame()
    for ctx in (
        _ctx(tmp_path),  # nothing at all (the Apps page preview)
        _ctx(tmp_path, database, data_paths),  # no item chosen
        _ctx(tmp_path, database, data_paths, "0" * 32),  # the photo was deleted
        _ctx(tmp_path, database, data_paths, "not-an-id"),
    ):
        frame = render_to_frame(P.APP, ctx)
        assert np.array_equal(frame, expected)
    colours = set(np.unique(expected).tolist())
    assert {BLACK, WHITE, YELLOW, RED, BLUE, GREEN} <= colours
    assert (expected == WHITE).mean() > 0.6  # a calm, mostly white card
    # the card is composed in exact palette colours: no dither speckle
    assert np.array_equal(P.no_photos_card(FONTS).to_frame(), expected)


def test_card_preview_image(tmp_path: Path) -> None:
    """Writes the card for a human to look at; also checks the text fits."""
    card = P.no_photos_card(FONTS)
    frame = card.to_frame()
    rows = np.where((frame != WHITE).any(axis=1))[0]
    cols = np.where((frame[:-12] != WHITE).any(axis=0))[0]
    assert rows.min() >= 40 and cols.min() >= 40 and cols.max() <= 560
    indices_to_image(frame).save(tmp_path / "photos-empty.png")


def test_missing_master_is_an_app_error(
    tmp_path: Path, database: Database, data_paths: DataPaths, photo_factory: PhotoFactory
) -> None:
    photo = photo_factory(Image.new("RGB", (90, 60)))
    pipeline.master_file(photo, data_paths).unlink()
    with pytest.raises(AppError, match="missing"):
        P.APP.render(_ctx(tmp_path, database, data_paths, photo.id))
    with pytest.raises(AppError, match="paths"):
        P.APP.render(_ctx(tmp_path, database, None, photo.id))


@pytest.mark.parametrize("wrong", [None, {}, {"default_recipe": dict(DEFAULT_RECIPE)}])
def test_without_the_settings_snapshot_is_an_app_error(
    tmp_path: Path,
    database: Database,
    data_paths: DataPaths,
    photo_factory: PhotoFactory,
    wrong: object,
) -> None:
    # a wiring mistake must be loud, not a render with the default palette
    # and recipe under a key the Photos page never shows
    photo = photo_factory(Image.new("RGB", (90, 60), (200, 30, 30)))
    ctx = _ctx(tmp_path, database, data_paths, photo.id)
    ctx.app_settings = wrong
    with pytest.raises(AppError, match="Settings"):
        P.APP.render(ctx)
    assert RenderCache(data_paths).keys(photo.id) == []
    # the "no photos" card needs no settings
    assert P.APP.render(_ctx(tmp_path, database, data_paths, None)).shape == (400, 600)
