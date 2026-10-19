"""The native dither engine (inkframe/render/dither.c) and its ctypes wrapper.

The C library must be built first (``scripts/build_dither.ps1`` on the
desktop, ``scripts/build_dither.sh`` or a deploy on the Pi).  A missing or
stale library fails every test in this module with the build command; it is
never skipped, because a silently skipped engine would hide a broken deploy.
The failure comes from a fixture, not from import time, so a plain ``pytest``
run still collects and runs every other module.
"""

from __future__ import annotations

import ctypes
import decimal
import json
import math
import shutil
import sys
import time

import numpy as np
import pytest

from inkframe.config import DEFAULT_RECIPE, RECIPE_RANGES
from inkframe.display.palette import MEASURED_DEFAULT, N_COLORS, NOMINAL, PANEL_HEIGHT, PANEL_WIDTH
from inkframe.render import dither as dm
from tests import dither_reference as ref


@pytest.fixture(scope="module", autouse=True)
def lib() -> ctypes.CDLL:
    """The loaded engine.  Without it every test here fails with the build command."""
    try:
        return dm.load_library()
    except dm.DitherError as exc:  # DitherLibraryMissing included
        pytest.fail(f"native dither library unavailable: {exc}", pytrace=False)


PALETTES = {"nominal": NOMINAL, "measured": MEASURED_DEFAULT}
ERROR_DIFFUSION = [m.id for m in dm.METHODS if m.kind == "error_diffusion"]
ORDERED = [m.id for m in dm.METHODS if m.kind == "ordered"]
OTHER_KERNELS = [m for m in ERROR_DIFFUSION if m not in ("floyd_steinberg", "atkinson")]
PLAN_ORDER = [
    "none", "floyd_steinberg", "atkinson", "jarvis", "stucki", "burkes", "sierra",
    "sierra_two_row", "sierra_lite", "bayer_4", "bayer_8", "blue_noise",
]  # fmt: skip


def synthetic(h: int, w: int, seed: int = 0) -> np.ndarray:
    """Gradient in all three channels plus seeded noise that saturates at 0/255."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    grad = np.stack(
        [
            xx * 255 // max(w - 1, 1),
            yy * 255 // max(h - 1, 1),
            (xx + yy) * 255 // max(w + h - 2, 1),
        ],
        axis=-1,
    )
    noise = rng.integers(-70, 71, size=(h, w, 3))
    return np.clip(grad + noise, 0, 255).astype(np.uint8)


def colour_gradient(h: int = 80, w: int = 120) -> np.ndarray:
    """A smooth two-axis colour gradient (no noise)."""
    yy, xx = np.mgrid[0:h, 0:w]
    r = 40 + xx * 200 // (w - 1)
    g = 200 - yy * 150 // (h - 1)
    b = 60 + (xx + yy) * 160 // (w + h - 2)
    return np.stack([r, g, b], axis=-1).astype(np.uint8)


def thresholds_for(method: str) -> np.ndarray:
    if method == "bayer_4":
        return ref.bayer_thresholds(4)
    if method == "bayer_8":
        return ref.bayer_thresholds(8)
    return dm.blue_noise_matrix()


def reference(img, palette, method, serpentine, strength, distance="rgb") -> np.ndarray:
    pal = palette.as_array()
    kind = dm.get_method(method).kind
    if kind == "nearest":
        return ref.nearest_image(img, pal, distance)
    if kind == "ordered":
        return ref.ordered(img, pal, thresholds_for(method), strength, distance)
    return ref.error_diffusion(img, pal, method, serpentine, strength, distance)


# --- (1) library and registry ---------------------------------------------------------


def test_library_abi_and_method_names(lib) -> None:
    assert lib.dither_abi_version() == dm.ABI_VERSION == 2
    assert [m.id for m in dm.METHODS] == PLAN_ORDER
    for i, m in enumerate(dm.METHODS):
        assert m.c_id == i
        assert lib.dither_method_name(m.c_id).decode() == m.id
        assert m.kind in dm.KINDS
        assert m.name and m.description and "\n" not in m.description
    assert lib.dither_method_name(-1) is None
    assert lib.dither_method_name(len(dm.METHODS)) is None
    assert dm.METHOD_IDS == tuple(PLAN_ORDER)
    assert {m.kind for m in dm.METHODS} == set(dm.KINDS)


def test_registry_agrees_with_recipe_settings() -> None:
    assert (dm.STRENGTH_MIN, dm.STRENGTH_MAX) == RECIPE_RANGES["strength"]
    assert DEFAULT_RECIPE["method"] in dm.METHOD_IDS
    assert DEFAULT_RECIPE["strength"] == dm.DEFAULT_STRENGTH
    assert DEFAULT_RECIPE["distance"] in dm.DISTANCES


def test_missing_library_message_names_the_build_command(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(dm, "_lib", None)
    monkeypatch.setattr(dm, "LIBRARY_PATH", tmp_path / "nope" / dm.LIBRARY_PATH.name)
    with pytest.raises(dm.DitherLibraryMissing) as ei:
        dm.dither(synthetic(4, 4), NOMINAL, "none")
    assert dm.build_command() in str(ei.value)
    assert isinstance(ei.value, dm.DitherError)


def _load_private_copy(tmp_path) -> ctypes.CDLL:
    """A second, independent instance of the library, with its own static state."""
    copy = tmp_path / ("fresh_" + dm.LIBRARY_PATH.name)
    shutil.copyfile(dm.LIBRARY_PATH, copy)
    fresh = ctypes.CDLL(str(copy))
    fresh.dither.argtypes = dm.load_library().dither.argtypes
    fresh.dither_rgb16_to_lab.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    fresh.dither_ordered_spread.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    return fresh


def _unload(handle: int) -> None:
    import _ctypes

    if sys.platform == "win32":
        _ctypes.FreeLibrary(handle)
    else:
        _ctypes.dlclose(handle)


def test_lab_needs_dither_init_first(tmp_path) -> None:
    """No lazy table build: a Lab call before dither_init() is an error (-8),
    so there is no unsynchronised first-use initialisation to race on."""
    fresh = _load_private_copy(tmp_path)
    try:
        img = np.ascontiguousarray(synthetic(4, 5))
        pal = NOMINAL.as_array()
        out = np.zeros((4, 5), np.uint8)
        sentinel = out.copy()

        def run(dist: int) -> int:
            return fresh.dither(
                img.ctypes.data, 5, 4, pal.ctypes.data, 6, 1, 1, 0.85, dist, None, 0,
                out.ctypes.data,
            )  # fmt: skip

        lab = np.zeros(3, np.int32)
        assert run(1) == -8
        np.testing.assert_array_equal(out, sentinel)  # untouched on error
        assert fresh.dither_rgb16_to_lab(lab.ctypes.data, 1, lab.ctypes.data) == -8
        assert fresh.dither_ordered_spread(pal.ctypes.data, 6, 1) == -8
        assert run(0) == 0  # rgb needs no tables
        assert fresh.dither_ordered_spread(pal.ctypes.data, 6, 0) == 96 * 16
        assert fresh.dither_init() == 0
        assert fresh.dither_init() == 0  # idempotent
        assert run(1) == 0
        np.testing.assert_array_equal(out, dm.dither(img, NOMINAL, "floyd_steinberg"))
    finally:
        _unload(fresh._handle)


# --- (2) exact match against the pure-Python reference -----------------------------------


@pytest.mark.parametrize("palette", list(PALETTES))
@pytest.mark.parametrize("strength", [1.0, 0.7])
@pytest.mark.parametrize("serpentine", [True, False])
@pytest.mark.parametrize("method", ["none", "floyd_steinberg", "atkinson"])
def test_exact_match_reference_core(method, serpentine, strength, palette) -> None:
    img = synthetic(16, 24)
    pal = PALETTES[palette]
    got = dm.dither(img, pal, method, serpentine=serpentine, strength=strength, distance="rgb")
    want = reference(img, pal, method, serpentine, strength)
    np.testing.assert_array_equal(got, want)


@pytest.mark.parametrize("serpentine", [True, False])
@pytest.mark.parametrize("method", OTHER_KERNELS)
def test_exact_match_reference_other_kernels(method, serpentine) -> None:
    # odd sizes so the serpentine's last row runs right-to-left and the
    # kernels' two-pixel reach hits both edges
    img = synthetic(17, 31, seed=3)
    for pal in PALETTES.values():
        got = dm.dither(img, pal, method, serpentine=serpentine, strength=0.85, distance="rgb")
        np.testing.assert_array_equal(got, reference(img, pal, method, serpentine, 0.85))


@pytest.mark.parametrize("strength", [1.0, 0.85, 0.5])
@pytest.mark.parametrize("method", ORDERED)
def test_exact_match_reference_ordered(method, strength) -> None:
    img = synthetic(66, 70, seed=4)  # > 64 both ways: the blue-noise tile wraps
    for pal in PALETTES.values():
        got = dm.dither(img, pal, method, strength=strength, distance="rgb")
        np.testing.assert_array_equal(got, reference(img, pal, method, True, strength))


@pytest.mark.parametrize("method", ["none", "floyd_steinberg", "jarvis", "atkinson", "bayer_8"])
def test_exact_match_reference_lab(method) -> None:
    """The reference mirrors the integer Lab pipeline too, including the
    interpolated (fractional) values error diffusion produces."""
    img = synthetic(12, 20, seed=5)
    for pal in PALETTES.values():
        got = dm.dither(img, pal, method, serpentine=True, strength=0.85, distance="lab")
        np.testing.assert_array_equal(got, reference(img, pal, method, True, 0.85, "lab"))


def test_strength_conversion_matches_c_rounding() -> None:
    assert ref.strength_q8(1.0) == 256
    assert ref.strength_q8(0.5) == 128
    assert ref.strength_q8(0.85) == 218  # float32(0.85) * 256 = 217.6000061
    assert ref.strength_q8(0.7) == 179


# --- (3) Lab -------------------------------------------------------------------------------


@pytest.mark.parametrize("palette", list(PALETTES))
def test_lab_nearest_matches_float_reference(palette) -> None:
    rng = np.random.default_rng(7)
    colors = rng.integers(0, 256, size=(2000, 3), dtype=np.uint8)
    pal = PALETTES[palette].as_array()
    got = dm.dither(colors.reshape(40, 50, 3), pal, "none", distance="lab").ravel()

    d = np.linalg.norm(ref.srgb_to_lab(colors)[:, None, :] - ref.srgb_to_lab(pal)[None], axis=2)
    want = np.argmin(d, axis=1)
    two = np.sort(d, axis=1)[:, :2]
    clear = (two[:, 1] - two[:, 0]) >= 0.5
    mismatches = int(np.count_nonzero((got != want) & clear))
    print(
        f"\nlab nearest ({palette}): {mismatches} mismatches of {int(clear.sum())} "
        f"unambiguous colours ({int((~clear).sum())} within 0.5 dE of a tie)"
    )
    assert mismatches < 0.01 * clear.sum()


def test_engine_lab_close_to_float_reference() -> None:
    rng = np.random.default_rng(8)
    greys = np.repeat(np.arange(256, dtype=np.uint8)[:, None], 3, axis=1)
    colors = np.concatenate(
        [greys, rng.integers(0, 256, size=(4096, 3), dtype=np.uint8),
         NOMINAL.as_array(), MEASURED_DEFAULT.as_array()]
    )  # fmt: skip
    de = np.linalg.norm(dm.engine_lab(colors) - ref.srgb_to_lab(colors), axis=1)
    print(f"\nengine Lab vs float: max dE {de.max():.4f}, mean {de.mean():.4f}")
    # over all 2^24 colours: max 0.0866 at (253, 0, 3), mean 0.0176
    # (python scripts/dither_stats.py --lab-cube)
    assert de.max() < 0.09
    assert de.mean() < 0.025
    np.testing.assert_array_equal(dm.engine_lab(np.array([[255, 255, 255]])), [[100.0, 0.0, 0.0]])
    np.testing.assert_array_equal(dm.engine_lab(np.array([[0, 0, 0]])), [[0.0, 0.0, 0.0]])


def test_engine_lab_equals_integer_reference() -> None:
    """Exact integer Lab, including the fractional (interpolated) values error
    diffusion feeds the search and the clamp of out-of-gamut values."""
    rng = np.random.default_rng(9)
    il = ref.int_lab()
    colors = rng.integers(0, 256, size=(2000, 3), dtype=np.uint8)
    want = np.array([il.lab(tuple(int(c) * ref.SCALE for c in col)) for col in colors]) / 256.0
    np.testing.assert_array_equal(dm.engine_lab(colors), want)
    scaled = rng.integers(ref.VMIN, ref.VMAX + 1, size=(3000, 3))
    scaled[:1000] = rng.integers(0, ref.VMAX8 + 1, size=(1000, 3))  # mostly in gamut
    want = np.array([il.lab(tuple(int(c) for c in col)) for col in scaled]) / 256.0
    np.testing.assert_array_equal(dm.engine_lab(scaled, scaled=True), want)


def test_lab_tables_are_far_from_rounding_boundaries() -> None:
    """The determinism argument for the Lab tables, checked.

    dither.c builds g_lin[256] and g_flab[4097] with libm pow/cbrt in double
    and rounds each value to an integer.  Recompute every exact value with 50
    significant digits: if none lies within 1e-6 of a .5 boundary, any libm
    that is accurate to better than ~1e-11 relative (glibc and the UCRT are
    within an ulp, ~1e-16) rounds every entry identically.
    """
    ctx = decimal.Context(prec=50)
    D = decimal.Decimal
    one_third = ctx.divide(D(1), D(3))
    eps = ctx.divide(D(216), D(24389))
    kappa = ctx.divide(D(24389), D(27))
    margins = []
    for i in range(256):
        c = ctx.divide(D(i), D(255))
        if c <= D("0.04045"):
            lin = ctx.divide(c, D("12.92"))
        else:
            lin = ctx.power(ctx.divide(c + D("0.055"), D("1.055")), D("2.4"))
        margins.append(lin * 65536)
    for j in range(4097):
        t = ctx.divide(D(j), D(4096))
        f = ctx.power(t, one_third) if t > eps else ctx.divide(kappa * t + 16, D(116))
        margins.append(f * 65536)
    worst = min(
        abs((v - v.to_integral_value(rounding=decimal.ROUND_FLOOR)) - D("0.5")) for v in margins
    )
    print(f"\nLab tables: closest entry is {float(worst):.3e} from a rounding boundary")
    assert worst > D("1e-6")
    # and the comparisons against the piecewise thresholds are not borderline either
    assert all(abs(D(i) / D(255) - D("0.04045")) > D("1e-4") for i in range(256))
    assert all(abs(D(j) / D(4096) - eps) > D("1e-5") for j in range(4097))


# --- (4) properties ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def prop_image() -> np.ndarray:
    return synthetic(80, 120, seed=11)


@pytest.mark.parametrize("distance", dm.DISTANCES)
@pytest.mark.parametrize("method", dm.METHOD_IDS)
def test_output_shape_dtype_range_and_determinism(method, distance, prop_image) -> None:
    a = dm.dither(prop_image, MEASURED_DEFAULT, method, distance=distance)
    assert a.dtype == np.uint8 and a.shape == prop_image.shape[:2]
    assert a.max() < N_COLORS
    b = dm.dither(prop_image, MEASURED_DEFAULT, method, distance=distance)
    np.testing.assert_array_equal(a, b)
    if method != "none":  # a dither uses more than one colour on this image
        assert len(np.unique(a)) > 2


@pytest.mark.parametrize("distance", dm.DISTANCES)
@pytest.mark.parametrize("method", dm.METHOD_IDS)
def test_flat_palette_colours_and_extremes_stay_flat(method, distance) -> None:
    """A flat area of a palette colour, of pure black or of pure white comes out
    as one colour, for both palettes and every method, strength and metric.

    Error diffusion gets this from zero error (and from errors that only push
    further beyond the palette's darkest or lightest entry).  The ordered
    methods get it from the palette-tied spread: the grey-axis offset never
    exceeds what keeps each of these anchors on its own nearest colour.
    """
    for pal in PALETTES.values():
        colours = [*pal.rgb, (0, 0, 0), (255, 255, 255)]
        img = np.empty((64, 64, 3), dtype=np.uint8)
        for c in colours:
            img[:] = c
            want = dm.dither(img[:1, :1], pal, "none", distance=distance)[0, 0]
            for strength in (0.5, 0.85, 1.0):
                out = dm.dither(img, pal, method, strength=strength, distance=distance)
                assert (out == want).all(), (pal.name, c, strength, np.bincount(out.ravel()))
        for i, c in enumerate(pal.rgb):  # a palette colour is its own nearest colour
            img[:] = c
            assert (dm.dither(img[:1, :1], pal, "none", distance=distance) == i).all()


def test_ordered_spread_is_tied_to_the_palette() -> None:
    # nominal: its anchors flip only at 127.4 (rgb) / 118.9 (lab) levels -> the 96 cap
    assert dm.ordered_spread(NOMINAL, "rgb") == dm.ordered_spread(NOMINAL, "lab") == 96.0
    assert dm.ORDERED_SPREAD_MAX == ref.ORDERED_SPREAD == 96
    # measured: black <-> green in rgb (37.3125 levels), green in lab (77.5)
    assert dm.ordered_spread(MEASURED_DEFAULT, "rgb") == 597 / 16
    assert dm.ordered_spread(MEASURED_DEFAULT, "lab") == 1240 / 16
    # two greys one level apart: the bisector is half a level (8 units) away and
    # the tie at exactly 8 goes to index 0, so entry 1 flips at -8: spread 7 units
    close = np.array([[100, 100, 100], [101, 101, 101]], dtype=np.uint8)
    assert dm.ordered_spread(close, "rgb") == 7 / 16
    # the engine and the independent reference scan agree, also on arbitrary palettes
    rng = np.random.default_rng(13)
    cases = [NOMINAL.as_array(), MEASURED_DEFAULT.as_array(), close]
    cases += [rng.integers(0, 256, size=(n, 3), dtype=np.uint8) for n in (2, 6, 6, 16)]
    for pal in cases:
        for distance in dm.DISTANCES:
            want = ref.ordered_spread(pal, distance)
            assert dm.ordered_spread(pal, distance) == want / 16, (pal.tolist(), distance)
    with pytest.raises(ValueError, match="distance"):
        dm.ordered_spread(NOMINAL, "xyz")
    with pytest.raises(ValueError, match="palette"):
        dm.ordered_spread(np.zeros((6, 4), np.uint8))


def test_ordered_methods_still_dither_with_the_measured_palette() -> None:
    """The palette-tied spread keeps flat anchors flat without collapsing the
    ordered methods into nearest-colour posterisation."""
    ramp = np.repeat(np.repeat(np.arange(256, dtype=np.uint8)[None, :, None], 3, axis=2), 64, 0)
    for distance in dm.DISTANCES:
        for method in ORDERED:
            out = dm.dither(ramp, MEASURED_DEFAULT, method, distance=distance)
            mixed = sum(len(np.unique(out[:, x])) > 1 for x in range(256))
            assert mixed > 64, (method, distance, mixed)


@pytest.mark.parametrize("method", dm.METHOD_IDS)
def test_serpentine_only_matters_for_error_diffusion(method, prop_image) -> None:
    for distance in dm.DISTANCES:
        on = dm.dither(prop_image, MEASURED_DEFAULT, method, serpentine=True, distance=distance)
        off = dm.dither(prop_image, MEASURED_DEFAULT, method, serpentine=False, distance=distance)
        if method in ERROR_DIFFUSION:
            assert not np.array_equal(on, off)
            np.testing.assert_array_equal(on[0], off[0])  # row 0 scans left-to-right in both
        else:
            np.testing.assert_array_equal(on, off)


@pytest.mark.parametrize("method", dm.METHOD_IDS)
def test_strength_changes_output(method, prop_image) -> None:
    for distance in dm.DISTANCES:
        lo = dm.dither(prop_image, MEASURED_DEFAULT, method, strength=0.5, distance=distance)
        hi = dm.dither(prop_image, MEASURED_DEFAULT, method, strength=1.0, distance=distance)
        if method == "none":
            np.testing.assert_array_equal(lo, hi)
        else:
            assert not np.array_equal(lo, hi)


@pytest.mark.parametrize("method", dm.METHOD_IDS)
def test_lab_and_rgb_differ_on_a_colour_gradient(method) -> None:
    img = colour_gradient()
    for pal in PALETTES.values():
        rgb = dm.dither(img, pal, method, distance="rgb")
        lab = dm.dither(img, pal, method, distance="lab")
        assert not np.array_equal(rgb, lab), pal.name


def test_ties_go_to_the_lower_index() -> None:
    # grey 128 is exactly 28 levels from both entries of this two-colour palette
    pal = np.array([[100, 100, 100], [156, 156, 156]], dtype=np.uint8)
    img = np.full((3, 5, 3), 128, dtype=np.uint8)
    assert (dm.dither(img, pal, "none", distance="rgb") == 0).all()
    assert (dm.dither(img, pal[::-1].copy(), "none", distance="rgb") == 0).all()
    # duplicate palette entries: the first one always wins
    dup = np.array([[0, 0, 0], [255, 0, 0], [255, 0, 0]], dtype=np.uint8)
    img = np.full((2, 2, 3), (250, 0, 0), dtype=np.uint8)
    for distance in dm.DISTANCES:
        assert (dm.dither(img, dup, "none", distance=distance) == 1).all()


def test_non_contiguous_input_and_generic_palettes(prop_image) -> None:
    flipped = prop_image[:, ::-1]
    assert not flipped.flags.c_contiguous
    np.testing.assert_array_equal(
        dm.dither(flipped, NOMINAL, "atkinson"),
        dm.dither(np.ascontiguousarray(flipped), NOMINAL, "atkinson"),
    )
    two = np.array([[0, 0, 0], [255, 255, 255]], dtype=np.uint8)
    out = dm.dither(prop_image, two, "floyd_steinberg", distance="rgb")
    assert set(np.unique(out)) == {0, 1}
    sixteen = np.random.default_rng(2).integers(0, 256, size=(16, 3), dtype=np.uint8)
    out = dm.dither(prop_image, sixteen, "jarvis")
    assert out.max() < 16
    # a Palette and its array give the same result
    np.testing.assert_array_equal(
        dm.dither(prop_image, MEASURED_DEFAULT, "sierra"),
        dm.dither(prop_image, MEASURED_DEFAULT.as_array(), "sierra"),
    )


#: (fill, palette greys, strength, test-pixel sources).  The image is all ``fill``
#: except three test pixels on its last row; rows above and the scan-side
#: neighbours run away until clamped at VMIN (fill 0) or VMAX (fill 255), so each
#: test pixel receives exactly the clamped pixels' error, and one of its sources
#: puts the error-adjusted value on (or one unit off) the two greys' midpoint.
CLAMP_PROBES = (
    (0, (50, 87), 1.0, (245, 246, 247)),  # VMIN + 1, VMIN +- 16
    (0, (68, 105), 0.85, (251, 252, 253)),  # VMIN - 1
    (255, (168, 205), 1.0, (8, 9, 10)),  # VMAX + 1, VMAX +- 16
    (255, (161, 191), 0.85, (12, 13, 14)),  # VMAX - 1
)
CLAMP_COLS = (8, 15, 22)


def clamp_probe(fill: int, srcs: tuple[int, int, int]) -> np.ndarray:
    img = np.full((5, 30, 3), fill, dtype=np.uint8)
    for x, s in zip(CLAMP_COLS, srcs, strict=True):
        img[-1, x] = s
    return img


def test_clamp_is_on_the_value_and_pinned_to_the_unit() -> None:
    """The error-adjusted value is clamped to [-0.5, 1.5] x 255 (VMIN, VMAX).

    Every kernel matches the reference on saturated input where the clamps
    bind; the probes are sensitive enough that moving either clamp by one
    scaled unit or one level, or clamping the incoming error to +-1.5 x 255
    instead of the value (the other reading of the plan), changes the output.
    """
    variants = {
        "VMIN+1": {"vmin": ref.VMIN + 1}, "VMIN-1": {"vmin": ref.VMIN - 1},
        "VMIN+16": {"vmin": ref.VMIN + 16}, "VMIN-16": {"vmin": ref.VMIN - 16},
        "VMAX+1": {"vmax": ref.VMAX + 1}, "VMAX-1": {"vmax": ref.VMAX - 1},
        "VMAX+16": {"vmax": ref.VMAX + 16}, "VMAX-16": {"vmax": ref.VMAX - 16},
        "error clamp": {"error_clamp": 3 * ref.VMAX8 // 2},
    }  # fmt: skip
    caught = dict.fromkeys(variants, False)
    for fill, greys, strength, srcs in CLAMP_PROBES:
        img = clamp_probe(fill, srcs)
        pal = np.array([[greys[0]] * 3, [greys[1]] * 3], dtype=np.uint8)
        for method in ERROR_DIFFUSION:
            for serpentine in (True, False):
                got = dm.dither(
                    img, pal, method, serpentine=serpentine, strength=strength, distance="rgb"
                )
                want = ref.error_diffusion(img, pal, method, serpentine, strength)
                np.testing.assert_array_equal(got, want, err_msg=f"{method} {serpentine} {greys}")
        base = ref.error_diffusion(img, pal, "floyd_steinberg", False, strength)
        for name, kw in variants.items():
            alt = ref.error_diffusion(img, pal, "floyd_steinberg", False, strength, **kw)
            caught[name] |= bool((alt != base).any())
    assert all(caught.values()), caught

    # By hand, first probe, Floyd-Steinberg at strength 1: a clamped pixel pushes
    # e = VMIN - 50 * 16 = -2840; a test pixel collects 16/16 of it, so
    # v = 16 * src - 2840 = 1080, 1096, 1112 against the midpoint 8 * (50 + 87) = 1096:
    # below, a tie (lower index wins), above.
    fill, greys, strength, srcs = CLAMP_PROBES[0]
    pal = np.array([[greys[0]] * 3, [greys[1]] * 3], dtype=np.uint8)
    img = clamp_probe(fill, srcs)
    out = dm.dither(img, pal, "floyd_steinberg", serpentine=False, strength=1.0, distance="rgb")
    assert out[-1, list(CLAMP_COLS)].tolist() == [0, 0, 1]


def test_error_diffusion_preserves_mean_tone() -> None:
    """At strength 1.0 Floyd-Steinberg on a flat mid grey with a black/white
    palette lands within a few percent of 50 % white (atkinson keeps less)."""
    img = np.full((60, 90, 3), 128, dtype=np.uint8)
    pal = np.array([[0, 0, 0], [255, 255, 255]], dtype=np.uint8)
    out = dm.dither(img, pal, "floyd_steinberg", strength=1.0, distance="rgb")
    assert abs(out.mean() - 128 / 255) < 0.03


# --- (5) errors ------------------------------------------------------------------------------


def test_unknown_method_lists_valid_ids() -> None:
    with pytest.raises(ValueError) as ei:
        dm.dither(synthetic(4, 4), NOMINAL, "floyd")
    for mid in dm.METHOD_IDS:
        assert mid in str(ei.value)


@pytest.mark.parametrize(
    "bad",
    [
        np.zeros((4, 4), np.uint8),
        np.zeros((4, 4, 4), np.uint8),
        np.zeros((4, 4, 3), np.float32),
        np.zeros((0, 4, 3), np.uint8),
        [[[0, 0, 0]]],
    ],
    ids=["2d", "rgba", "float", "empty", "list"],
)
def test_bad_image_rejected(bad) -> None:
    with pytest.raises(ValueError, match="rgb"):
        dm.dither(bad, NOMINAL, "atkinson")


@pytest.mark.parametrize(
    "strength", [1.5, 0.49, -1.0, float("nan"), float("inf"), True, "0.8", None]
)
def test_bad_strength_rejected(strength) -> None:
    with pytest.raises(ValueError, match="strength"):
        dm.dither(synthetic(4, 4), NOMINAL, "atkinson", strength=strength)


def test_other_bad_arguments() -> None:
    img = synthetic(4, 4)
    with pytest.raises(ValueError, match="distance"):
        dm.dither(img, NOMINAL, "atkinson", distance="xyz")
    with pytest.raises(ValueError, match="serpentine"):
        dm.dither(img, NOMINAL, "atkinson", serpentine=1)
    with pytest.raises(ValueError, match="palette"):
        dm.dither(img, np.zeros((6, 4), np.uint8), "atkinson")
    with pytest.raises(ValueError, match="colours"):
        dm.dither(img, np.zeros((17, 3), np.uint8), "atkinson")
    with pytest.raises(ValueError, match="colours"):
        dm.dither(img, np.zeros((1, 3), np.uint8), "atkinson")
    with pytest.raises(ValueError, match="uint8"):
        dm.dither(img, np.zeros((6, 3), np.int64), "atkinson")
    with pytest.raises(ValueError, match="palette"):
        dm.dither(img, [(0, 0, 0), (255, 255, 255)], "atkinson")


def test_c_error_codes(lib) -> None:
    img = np.ascontiguousarray(synthetic(4, 5))
    pal = NOMINAL.as_array()
    out = np.zeros((4, 5), np.uint8)
    bn = dm.blue_noise_matrix()

    def call(**kw):
        a = dict(
            rgb=img.ctypes.data, w=5, h=4, pal=pal.ctypes.data, n=6, method=1, serp=1,
            strength=0.85, dist=1, thr=None, thr_n=0, out=out.ctypes.data,
        )  # fmt: skip
        a.update(kw)
        return lib.dither(
            a["rgb"], a["w"], a["h"], a["pal"], a["n"], a["method"], a["serp"],
            a["strength"], a["dist"], a["thr"], a["thr_n"], a["out"],
        )  # fmt: skip

    assert call() == 0
    assert call(rgb=None) == call(pal=None) == call(out=None) == -1
    assert call(w=0) == call(h=-1) == call(w=16385) == -2
    assert call(n=1) == call(n=17) == -3
    assert call(method=-1) == call(method=12) == -4
    assert call(dist=2) == call(dist=-1) == -5
    assert call(method=11) == -6
    assert call(method=11, thr=bn.ctypes.data, thr_n=32) == -6
    assert call(method=11, thr=bn.ctypes.data, thr_n=64) == 0
    assert call(strength=float("nan")) == -7
    # finite out-of-range strengths are clamped by C, not rejected
    for s, inside in ((5.0, 1.0), (-3.0, 0.5)):
        assert call(strength=s) == 0
        clamped = out.copy()
        assert call(strength=inside) == 0
        np.testing.assert_array_equal(out, clamped)
    lab = np.zeros(3, np.int32)
    assert lib.dither_rgb16_to_lab(None, 1, lab.ctypes.data) == -1
    assert lib.dither_rgb16_to_lab(lab.ctypes.data, 0, lab.ctypes.data) == -2
    assert lib.dither_ordered_spread(None, 6, 1) == -1
    assert lib.dither_ordered_spread(pal.ctypes.data, 1, 1) == -3
    assert lib.dither_ordered_spread(pal.ctypes.data, 17, 1) == -3
    assert lib.dither_ordered_spread(pal.ctypes.data, 6, 2) == -5
    assert lib.dither_ordered_spread(pal.ctypes.data, 6, 1) == 96 * 16
    with pytest.raises(ValueError):
        dm.engine_lab(np.array([[0, 0, 256]]))
    # engine_lab takes integers only: no silent truncation of floats or bools
    for bad in (np.array([[10.7, 0.0, 0.0]]), np.array([[True, False, True]])):
        with pytest.raises(ValueError, match="integers"):
            dm.engine_lab(bad)
    np.testing.assert_array_equal(
        dm.engine_lab(np.array([[10, 0, 0]], np.uint8)), dm.engine_lab([[10, 0, 0]])
    )


def _low_share(tex: np.ndarray) -> float:
    """Share of the DC-free power spectrum below 1/8 cycle per pixel."""
    t = tex.astype(np.float64)
    p = np.abs(np.fft.fft2(t - t.mean())) ** 2
    f = np.hypot(np.fft.fftfreq(t.shape[0])[:, None], np.fft.fftfreq(t.shape[1])[None, :])
    return float(p[(f > 0) & (f < 0.125)].sum() / p[f > 0].sum())


def test_blue_noise_texture() -> None:
    tex = dm.blue_noise_matrix()
    assert tex.shape == (64, 64) and tex.dtype == np.uint8
    assert not tex.flags.writeable
    assert dm.blue_noise_matrix() is tex  # loaded once
    counts = np.bincount(tex.ravel(), minlength=256)
    assert counts.min() == counts.max() == 16  # rank * 256 // 4096
    # blue: little power at low spatial frequencies compared with white noise
    rng = np.random.default_rng(0)
    white = np.mean([_low_share(rng.permutation(tex.ravel()).reshape(64, 64)) for _ in range(8)])
    ratio = _low_share(tex) / white
    print(f"\nblue noise low-frequency power vs white noise: {ratio:.4f}")
    assert ratio < 0.1


@pytest.mark.parametrize(
    "content", [None, b"", b"not an npy file", b"\x93NUMPY\x01\x00"], ids=lambda c: repr(c)[:12]
)
def test_blue_noise_texture_problems_raise_dither_error(monkeypatch, tmp_path, content) -> None:
    path = tmp_path / "blue_noise_64.npy"
    if content is not None:
        path.write_bytes(content)
    monkeypatch.setattr(dm, "BLUE_NOISE_PATH", path)
    monkeypatch.setattr(dm, "_blue_noise", None)
    with pytest.raises(dm.DitherError, match="blue_noise_64.npy"):
        dm.blue_noise_matrix()
    with pytest.raises(dm.DitherError):
        dm.dither(synthetic(4, 4), NOMINAL, "blue_noise")
    np.save(path, np.zeros((32, 32), np.uint8))  # readable, wrong shape
    with pytest.raises(dm.DitherError, match="64x64"):
        dm.blue_noise_matrix()


def test_bayer_matrix() -> None:
    b4 = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]
    np.testing.assert_array_equal(dm.bayer_matrix(4), np.array(b4) * 17)
    np.testing.assert_array_equal(dm.bayer_matrix(2), [[0, 170], [255, 85]])
    for n in (4, 8, 16):
        np.testing.assert_array_equal(dm.bayer_matrix(n), ref.bayer_thresholds(n))
        assert dm.bayer_matrix(n).dtype == np.uint8
        m = dm.bayer_matrix(n).astype(np.int64)
        assert m.min() == 0 and m.max() == 255
        assert len(np.unique(m)) == n * n
        # symmetric around 127.5 so ordered dithering has no tone bias
        assert m.mean() == pytest.approx(127.5, abs=0.01)
    for bad in (0, 1, 3, 6, 128, 4.0, True, np.bool_(True), np.float64(4.0), "4", None):
        with pytest.raises(ValueError):
            dm.bayer_matrix(bad)
    for n in (np.int64(4), np.uint8(8), np.int32(2)):  # any integer type
        np.testing.assert_array_equal(dm.bayer_matrix(n), dm.bayer_matrix(int(n)))


def test_cli_round_trip(tmp_path) -> None:
    from PIL import Image

    src = tmp_path / "in.png"
    dst = tmp_path / "out.png"
    Image.fromarray(colour_gradient(50, 70)).save(src)
    assert (
        dm.main([str(src), str(dst), "--method", "bayer_8", "--palette", "nominal", "--fit"]) == 0
    )
    with Image.open(dst) as im:
        assert im.size == (PANEL_WIDTH, PANEL_HEIGHT)
        colours = {c for _, c in im.convert("RGB").getcolors(maxcolors=64)}
    assert colours <= set(NOMINAL.rgb)
    assert dm.main([str(src), str(dst), "--strength", "1.5"]) == 2


def test_cli_reports_unreadable_input_and_output(tmp_path, capsys) -> None:
    from PIL import Image

    src = tmp_path / "in.png"
    Image.fromarray(colour_gradient(10, 12)).save(src)
    not_image = tmp_path / "notes.png"
    not_image.write_text("not an image", encoding="utf-8")
    for bad in (tmp_path / "missing.png", not_image, tmp_path):
        assert dm.main([str(bad), str(tmp_path / "out.png")]) == 2
        assert capsys.readouterr().err.startswith("error: cannot read")
    for bad in (tmp_path / "no" / "such" / "dir" / "out.png", tmp_path / "out.unknownext"):
        assert dm.main([str(src), str(bad)]) == 2
        assert capsys.readouterr().err.startswith("error: cannot write")


def test_cli_scales_16_bit_greyscale(tmp_path) -> None:
    """A 16-bit greyscale PNG is scaled to 8 bits, not clipped to white."""
    from PIL import Image

    wide = np.zeros((8, 12), dtype=np.uint16)
    wide[:, 4:8] = 0x8000  # mid grey
    wide[:, 8:] = 0xFFFF
    src, dst = tmp_path / "in16.png", tmp_path / "out.png"
    Image.fromarray(wide).save(src)
    with Image.open(src) as im:
        assert im.mode in dm._WIDE_INT_MODES  # Pillow keeps it 16-bit
    with dm._read_rgb(src) as rgb8:
        a = np.asarray(rgb8)
    assert a.shape == (8, 12, 3) and a.dtype == np.uint8
    np.testing.assert_array_equal(a[:, :4], 0)
    np.testing.assert_array_equal(a[:, 4:8], 128)
    np.testing.assert_array_equal(a[:, 8:], 255)
    assert dm.main([str(src), str(dst), "--method", "none", "--palette", "nominal"]) == 0
    with Image.open(dst) as im:
        out = np.asarray(im.convert("RGB"))
    np.testing.assert_array_equal(out[:, :4], 0)  # black stays black (was clipped to white)
    np.testing.assert_array_equal(out[:, 8:], 255)


# --- (6) timing ---------------------------------------------------------------------------------


def test_timing_600x400(capsys) -> None:
    img = np.random.default_rng(12).integers(
        0, 256, size=(PANEL_HEIGHT, PANEL_WIDTH, 3), dtype=np.uint8
    )
    dm.dither(img, MEASURED_DEFAULT, "atkinson")  # warm up caches
    rows, slow = [], []
    for m in dm.METHODS:
        cells = []
        for distance in dm.DISTANCES:
            best = math.inf
            for _ in range(3):
                t0 = time.perf_counter()
                dm.dither(img, MEASURED_DEFAULT, m.id, distance=distance)
                best = min(best, time.perf_counter() - t0)
            cells.append(best * 1000)
            if best >= 1.0:
                slow.append(f"{m.id}/{distance}: {best:.2f} s")
        rows.append(f"  {m.id:<16} {cells[0]:8.1f} {cells[1]:8.1f}")
    with capsys.disabled():
        print(f"\ndither timings, {PANEL_WIDTH}x{PANEL_HEIGHT}, best of 3 (ms):")
        print(f"  {'method':<16} {'rgb':>8} {'lab':>8}")
        print("\n".join(rows))
    assert not slow, slow


# --- (7) golden hashes ---------------------------------------------------------------------------


def check_golden(path) -> list[str]:
    """Compare every entry of a golden file with the engine; returns the problems."""
    from tests import gen_dither_golden as gen

    data = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    img = gen.make_golden_image()
    if data["image"]["sha256"] != gen.sha256(img):
        return [
            "the golden input image changed (tests/gen_dither_golden.py or numpy's PCG64 "
            "stream); regenerate the golden file deliberately"
        ]
    for name, rgb in data["palettes"].items():
        if [list(c) for c in gen.PALETTES[name].rgb] != rgb:
            problems.append(f"palette {name!r} differs from the one the golden file was made with")
    if problems:
        return problems
    for key, want in sorted(data["hashes"].items()):
        method, serp, dist, pal, strength = gen.parse_key(key, data["strength"])
        out = dm.dither(
            img, gen.PALETTES[pal], method, serpentine=serp, strength=strength, distance=dist
        )
        if gen.sha256(out) != want:
            problems.append(key)
    return problems


def test_golden_hashes() -> None:
    from tests import gen_dither_golden as gen

    if not gen.GOLDEN_PATH.is_file():
        pytest.skip("golden file not generated yet (python -m tests.gen_dither_golden)")
    problems = check_golden(gen.GOLDEN_PATH)
    assert not problems, f"{len(problems)} golden mismatches: {problems[:20]}"
