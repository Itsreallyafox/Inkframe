"""Patterns: every family, variant and setting renders an exact six-colour frame,
deterministically, quickly, and within its palette mood; the test card is complete."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from inkframe.apps import load_app, patterns, patterns_testcard
from inkframe.apps.base import (
    App,
    AppError,
    FontLibrary,
    RenderContext,
    render_to_frame,
    schema_defaults,
    validate_settings,
)
from inkframe.apps.patterns import APP, FAMILIES, compose, pick_family, resolved_settings
from inkframe.apps.patterns_common import (
    FILL,
    LINE,
    Perlin,
    W,
    contrast,
    pick_scheme,
)
from inkframe.display.palette import (
    BLACK,
    BLUE,
    GREEN,
    MEASURED_DEFAULT,
    RED,
    WHITE,
    YELLOW,
    Palette,
)

FIXTURES = Path(__file__).parent / "fixtures" / "patterns"
FONTS = FontLibrary()
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
SEEDS = (1, 2, 3)


@pytest.fixture(autouse=True)
def _no_silent_fallback(request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture):
    """The app falls back to another family when one raises, which keeps the
    panel alive but would also hide a bug from these tests: any fallback
    fails the test (except in the test that exercises it)."""
    caplog.set_level(logging.ERROR, logger="inkframe.apps.patterns")
    yield
    if "falls_back" in request.node.name:
        return
    errors = [r for r in caplog.records if r.name == "inkframe.apps.patterns"]
    assert not errors, errors[0].getMessage()


def _ctx(tmp_path: Path, settings: dict | None = None, **kw) -> RenderContext:
    return RenderContext(
        settings=validate_settings(APP.settings_schema, settings or {}),
        now=kw.pop("now", NOW),
        cache_dir=tmp_path,
        fonts=FONTS,
        online=False,
        **kw,
    )


def _frame(tmp_path: Path, settings: dict | None = None, **kw) -> np.ndarray:
    return render_to_frame(APP, _ctx(tmp_path, settings, **kw))


def _valid(frame: np.ndarray) -> None:
    assert frame.shape == (400, 600) and frame.dtype == np.uint8
    assert int(frame.max()) <= 5
    assert len(np.unique(frame)) >= 2  # never a blank card


# --- contract and settings ----------------------------------------------------------


def test_app_satisfies_contract() -> None:
    assert isinstance(APP, App)
    assert APP.id == "patterns" == patterns.__name__.rsplit(".", 1)[1]
    assert load_app("patterns") is APP
    assert APP.needs_dither is False and APP.variants == 3 and APP.name == "Patterns"


def test_settings_schema_defaults_validate() -> None:
    defaults = schema_defaults(APP.settings_schema)
    assert defaults == {"family": "random", "density": 3, "palette_mood": "full", "caption": True}
    assert validate_settings(APP.settings_schema, defaults) == defaults
    keys = {f.key for f in APP.settings_schema}
    assert keys == {"family", "density", "palette_mood", "caption"}
    family = next(f for f in APP.settings_schema if f.key == "family")
    assert {v for v, _ in family.options} == {"random", "test_card", *FAMILIES}
    with pytest.raises(ValueError):
        validate_settings(APP.settings_schema, {"density": 9})
    with pytest.raises(ValueError):
        validate_settings(APP.settings_schema, {"palette_mood": "neon"})
    # rendering is tolerant: bad or missing values fall back to defaults, never raise
    assert resolved_settings({"density": "lots", "family": "nope", "extra": 1}) == defaults
    assert resolved_settings(None) == defaults


# --- families -------------------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_every_family_renders_exact_frames(tmp_path: Path, family: str, seed: int) -> None:
    frame = _frame(tmp_path, {"family": family}, seed=seed)
    _valid(frame)
    # the app path did not fall back to another family: it matches a direct compose
    canvas, plate = compose(family, seed, 0, resolved_settings({"family": family}), FONTS)
    assert plate.title.lower() == family
    assert np.array_equal(canvas.to_frame(), frame)


@pytest.mark.parametrize("family", FAMILIES)
def test_many_seeds_settings_and_draws_never_fail(family: str) -> None:
    """A broad sweep straight through ``compose`` (no fallback): every density,
    mood and caption setting over 30 seeds and both draws."""
    rng = np.random.default_rng(2026)
    for seed in range(30):
        settings = resolved_settings(
            {
                "family": family,
                "density": int(rng.integers(1, 6)),
                "palette_mood": str(rng.choice(["full", "duotone", "warm", "cool"])),
                "caption": bool(rng.random() < 0.8),
            }
        )
        for draw in (0, 1):
            canvas, plate = compose(family, seed, draw, settings, FONTS)
            _valid(canvas.to_frame())
            x0, y0, x1, y1 = plate.box
            assert 0 <= x0 < x1 <= 600 and 0 <= y0 < y1 <= 400


@pytest.mark.parametrize("family", FAMILIES)
def test_same_seed_same_frame_different_seed_different_frame(tmp_path: Path, family: str) -> None:
    a = _frame(tmp_path, {"family": family}, seed=41)
    b = _frame(tmp_path, {"family": family}, seed=41)
    c = _frame(tmp_path, {"family": family}, seed=42)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)


@pytest.mark.parametrize("family", FAMILIES)
def test_density_changes_the_composition(tmp_path: Path, family: str) -> None:
    sparse = _frame(tmp_path, {"family": family, "density": 1}, seed=5)
    dense = _frame(tmp_path, {"family": family, "density": 5}, seed=5)
    _valid(sparse)
    _valid(dense)
    assert not np.array_equal(sparse, dense)


def test_density_scales_elements() -> None:
    """Density is not just a different draw: it changes element counts and sizes."""
    from inkframe.apps import patterns_flow, patterns_truchet

    assert patterns_truchet._TILE[1] > patterns_truchet._TILE[5]
    assert patterns_flow._SEEDS[1] < patterns_flow._SEEDS[5]
    lines = []
    for density in (1, 5):
        rng = np.random.default_rng(3)
        n = patterns_flow._SEEDS[density]
        seeds = np.stack([rng.uniform(30, 570, n), rng.uniform(30, 340, n)], axis=1)
        lines.append(
            len(
                patterns_flow.trace(
                    lambda x, y: (np.ones_like(x), np.zeros_like(y)),
                    seeds,
                    (24, 24, 576, 348),
                    step=3.0,
                    max_steps=np.full(n, 8),
                    d_sep=0.0,
                    batch=n,
                    min_points=2,
                )
            )
        )
    assert lines[0] < lines[1]


_MOOD_SETS = {
    "warm": {BLACK, WHITE, YELLOW, RED},
    "cool": {BLACK, WHITE, BLUE, GREEN},
}


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("mood", ["duotone", "warm", "cool"])
def test_palette_mood_restricts_colours(tmp_path: Path, family: str, mood: str) -> None:
    seeds = (1, 2, 3, 4)
    frames = [_frame(tmp_path, {"family": family, "palette_mood": mood}, seed=s) for s in seeds]
    for frame in frames:
        _valid(frame)
        used = set(np.unique(frame).tolist())
        if mood == "duotone":
            assert len(used - {BLACK, WHITE}) <= 1 and WHITE in used, used
        else:
            assert used <= _MOOD_SETS[mood], used
    full = [_frame(tmp_path, {"family": family}, seed=s) for s in seeds]
    assert any(not np.array_equal(a, b) for a, b in zip(full, frames, strict=True))


def test_full_mood_uses_more_colours_than_duotone(tmp_path: Path) -> None:
    def colours(mood: str) -> set[int]:
        out: set[int] = set()
        for fam in FAMILIES:
            for s in (1, 2, 3):
                f = _frame(tmp_path, {"family": fam, "palette_mood": mood}, seed=s)
                out |= set(np.unique(f).tolist())
        return out

    assert colours("full") == {BLACK, WHITE, YELLOW, RED, BLUE, GREEN}
    assert len(colours("duotone")) >= 3  # different cards pick different accents


def test_schemes_keep_contrast() -> None:
    for mood in ("full", "duotone", "warm", "cool"):
        for s in range(40):
            rng = np.random.default_rng(s)
            sc = pick_scheme(rng, mood, grounds={WHITE: 1, YELLOW: 1, BLACK: 1}, n=3, need=LINE)
            assert all(contrast(i, sc.ground) >= LINE for i in sc.inks)
            if mood == "duotone":
                assert sc.ground == WHITE and len(sc.allowed - {BLACK, WHITE}) == 1
                assert set(sc.inks) == sc.allowed - {WHITE}  # black and its accent
            else:
                pairs = [(a, b) for k, a in enumerate(sc.inks) for b in sc.inks[k + 1 :]]
                assert all(contrast(a, b) >= FILL for a, b in pairs)


def test_caption_setting(tmp_path: Path) -> None:
    on = compose("flow", 7, 0, resolved_settings({"caption": True}), FONTS)
    off = compose("flow", 7, 0, resolved_settings({"caption": False}), FONTS)
    assert on[1].caption and not off[1].caption
    band_on = on[0].to_frame()[360:392, 24:200]
    assert (band_on == BLACK).sum() > 80  # the mono caption is there
    assert off[1].box[3] > on[1].box[3]  # without a caption the print uses the room


def test_caption_never_collides_with_the_number() -> None:
    from inkframe.apps.draw import Canvas
    from inkframe.apps.patterns_common import Plate

    c = Canvas()
    plate = Plate((200, 24, 400, 348), caption=True, title="Concentric", note="rectangles inverted")
    patterns.finish(c, plate, FONTS, 7)
    row = c.to_frame()[362:382, 200:400]
    ink_cols = np.flatnonzero((row == BLACK).any(axis=0))
    gaps = np.diff(ink_cols)
    assert gaps.max() >= 14  # a clear gap separates the title from the number


def test_noise_is_smooth_and_seeded() -> None:
    a = Perlin(np.random.default_rng(1), period=100.0, octaves=2)
    b = Perlin(np.random.default_rng(1), period=100.0, octaves=2)
    ys, xs = np.mgrid[0:400:4, 0:W:4].astype(float)
    va, vb = a(xs, ys), b(xs, ys)
    assert np.array_equal(va, vb)
    assert -1.0 < va.min() < 0 < va.max() < 1.0
    assert np.abs(np.diff(va, axis=1)).max() < 0.2  # no jumps between neighbours


# --- variants and seeding ---------------------------------------------------------------


def test_variants(tmp_path: Path) -> None:
    v0 = _frame(tmp_path, seed=7, variant=0)
    v1 = _frame(tmp_path, seed=7, variant=1)
    v2 = _frame(tmp_path, seed=7, variant=2)
    for f in (v0, v1, v2):
        _valid(f)
    assert not np.array_equal(v0, v1)
    assert np.array_equal(v2, _frame(tmp_path, seed=8, variant=2))  # the card ignores the seed
    assert np.array_equal(v0, _frame(tmp_path, seed=7))  # None = draw 0 of the setting
    # a second draw is always a different family
    assert all(pick_family(s, 0) != pick_family(s, 1) for s in range(200))
    assert {pick_family(s) for s in range(200)} == set(FAMILIES)
    # one configured family: variant 1 is another composition of that family
    t0 = _frame(tmp_path, {"family": "truchet"}, seed=7, variant=0)
    t1 = _frame(tmp_path, {"family": "truchet"}, seed=7, variant=1)
    assert not np.array_equal(t0, t1)


def test_seed_defaults_to_render_time(tmp_path: Path) -> None:
    a = _frame(tmp_path, now=NOW)
    b = _frame(tmp_path, now=NOW)
    c = _frame(tmp_path, now=NOW + timedelta(seconds=1))
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)
    ctx = _ctx(tmp_path, now=NOW)
    assert patterns.resolve_seed(ctx) == int(NOW.timestamp())
    assert patterns.resolve_seed(_ctx(tmp_path, seed=-5)) >= 0


# --- the test card ----------------------------------------------------------------------


def test_test_card_reachable_and_deterministic(tmp_path: Path) -> None:
    by_variant = _frame(tmp_path, seed=1, variant=2)
    by_setting = _frame(tmp_path, {"family": "test_card"}, seed=99)
    by_setting_v0 = _frame(tmp_path, {"family": "test_card"}, seed=3, variant=0)
    assert np.array_equal(by_variant, by_setting)
    assert np.array_equal(by_variant, by_setting_v0)
    assert set(np.unique(by_variant).tolist()) == {BLACK, WHITE, YELLOW, RED, BLUE, GREEN}


def test_test_card_swatches_labels_and_marks(tmp_path: Path) -> None:
    f = _frame(tmp_path, variant=2)
    k = patterns_testcard.KEYLINE
    for i, (x0, y0, x1, y1) in patterns_testcard.SWATCHES.items():
        assert x1 - x0 >= 170 and y1 - y0 >= 100  # large swatches, two rows of three
        inner = f[y0 + k : y1 - k, x0 + k : x1 - k]
        assert inner.size > 15000 and (inner == i).all(), f"swatch {i} not solid"
        ring = np.concatenate([f[y0, x0:x1], f[y1 - 1, x0:x1], f[y0:y1, x0], f[y0:y1, x1 - 1]])
        assert (ring == BLACK).all(), f"swatch {i} keyline"
        lx0, ly0, lx1, ly1 = patterns_testcard.LABELS[i]
        label = f[ly0:ly1, lx0:lx1]
        assert set(np.unique(label).tolist()) <= {BLACK, WHITE}
        assert (label == BLACK).sum() > 300, f"swatch {i} label missing"
    # swatch rows do not collide with their labels or with the bottom band
    for i in range(3):
        assert patterns_testcard.LABELS[i][3] <= patterns_testcard.SWATCHES[i + 3][1]
        assert patterns_testcard.LABELS[i + 3][3] <= patterns_testcard.BAND_TOP
    # orientation marks: a solid arrow in the top-left corner, the UP arrow at the top centre
    x0, y0, x1, y1 = patterns_testcard.MARK_TOP_LEFT
    assert (f[0:40, 16:40] == BLACK).sum() > 300  # the arrow itself
    assert (f[y0:y1, 44:x1] == BLACK).sum() > 150  # "TOP LEFT"
    assert (f[6:12, 25:30] == BLACK).any() and not (f[6:12, 16:20] == BLACK).any()  # points up
    x0, y0, x1, y1 = patterns_testcard.MARK_UP
    assert (f[y0:y1, x0:x1] == BLACK).sum() > 100
    # 50 % textures
    for name in ("stripes", "checker"):
        px0, py0, px1, py1 = patterns_testcard.PATCHES[name]
        patch = f[py0:py1, px0:px1]
        assert abs((patch == BLACK).mean() - 0.5) < 0.05, name
    # line set: 1 px and 2 px vertical lines exist in the bottom band
    band = f[
        patterns_testcard.BAND_TOP + 2, patterns_testcard.VLINES_X : patterns_testcard.HLINES_X
    ]
    runs = np.diff(np.flatnonzero(np.diff(np.r_[0, (band == BLACK).astype(int), 0])))[::2]
    assert {1, 2} <= set(runs.tolist())


def test_test_card_shows_the_measured_palette(tmp_path: Path) -> None:
    fixture = Palette.from_json((FIXTURES / "measured_palette.json").read_text(encoding="utf-8"))
    default = _frame(tmp_path, variant=2, measured_palette=MEASURED_DEFAULT)
    tuned = _frame(tmp_path, variant=2, measured_palette=fixture)
    diff = default != tuned
    assert diff.any()
    allowed = np.zeros_like(diff)
    for x0, y0, x1, y1 in patterns_testcard.HEX_LABELS.values():
        allowed[y0:y1, x0:x1] = True
    assert not (diff & ~allowed).any(), "only the measured-hex lines may change"
    for i, (x0, y0, x1, y1) in patterns_testcard.HEX_LABELS.items():
        assert diff[y0:y1, x0:x1].any(), f"hex label {i} did not change"


# --- robustness and speed ---------------------------------------------------------------


def test_a_failing_family_falls_back(tmp_path: Path, monkeypatch, caplog) -> None:
    def boom(rng, canvas, settings):
        raise RuntimeError("generative bug")

    monkeypatch.setitem(patterns.DRAW, "truchet", boom)
    with caplog.at_level(logging.ERROR, logger="inkframe.apps.patterns"):
        frame = _frame(tmp_path, {"family": "truchet"}, seed=3)
    _valid(frame)
    assert "truchet failed" in caplog.text
    for fam in FAMILIES:
        monkeypatch.setitem(patterns.DRAW, fam, boom)
    with pytest.raises(AppError):
        _frame(tmp_path, {"family": "truchet"}, seed=3)


@pytest.mark.parametrize("family", [*FAMILIES, "test_card"])
def test_render_time(tmp_path: Path, family: str) -> None:
    worst = 0.0
    for seed in SEEDS:
        for density in (3, 5):
            t = time.perf_counter()
            _frame(tmp_path, {"family": family, "density": density}, seed=seed)
            worst = max(worst, time.perf_counter() - t)
    assert worst < 3.0, f"{family}: {worst:.2f} s"
