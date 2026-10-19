"""Palette and controller packing (the shared contract in display/palette.py)."""

from __future__ import annotations

import numpy as np
import pytest

from inkframe.display.palette import (
    BLACK,
    BLUE,
    FRAME_BYTES,
    GREEN,
    MEASURED_DEFAULT,
    NAMES,
    NOMINAL,
    PANEL_CODES,
    PANEL_HEIGHT,
    PANEL_WIDTH,
    RED,
    WHITE,
    YELLOW,
    Palette,
    indices_to_image,
    indices_to_rgb,
    pack_frame,
    rgb_to_indices_exact,
    unpack_frame,
)


def random_frame(seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 6, size=(PANEL_HEIGHT, PANEL_WIDTH), dtype=np.uint8)


def test_index_order_and_panel_codes() -> None:
    assert (BLACK, WHITE, YELLOW, RED, BLUE, GREEN) == (0, 1, 2, 3, 4, 5)
    assert NAMES == ("black", "white", "yellow", "red", "blue", "green")
    assert list(PANEL_CODES) == [0x0, 0x1, 0x2, 0x3, 0x5, 0x6]


@pytest.mark.parametrize("rotation", [90, 270])
def test_pack_unpack_round_trip(rotation: int) -> None:
    frame = random_frame()
    packed = pack_frame(frame, rotation)
    assert isinstance(packed, bytes)
    assert len(packed) == FRAME_BYTES == 120000
    assert np.array_equal(unpack_frame(packed, rotation), frame)


def test_rotations_differ() -> None:
    frame = random_frame(2)
    assert pack_frame(frame, 90) != pack_frame(frame, 270)


def test_vendor_orientation_corner_rotation_90() -> None:
    """Vendor getbuffer rotates 90 deg CCW: landscape (x=599, y=0) is native (0, 0)."""
    frame = np.zeros((PANEL_HEIGHT, PANEL_WIDTH), dtype=np.uint8)  # all BLACK (code 0)
    frame[0, 599] = RED
    packed = pack_frame(frame, 90)
    assert packed[0] == 0x30  # high nibble = left native pixel
    assert packed.count(0) == FRAME_BYTES - 1


def test_vendor_orientation_corner_rotation_270() -> None:
    frame = np.zeros((PANEL_HEIGHT, PANEL_WIDTH), dtype=np.uint8)
    frame[0, 599] = RED
    assert pack_frame(frame, 270)[-1] == 0x03  # opposite corner, low nibble
    frame = np.zeros((PANEL_HEIGHT, PANEL_WIDTH), dtype=np.uint8)
    frame[399, 0] = GREEN
    assert pack_frame(frame, 270)[0] == 0x60


def test_pack_uses_panel_codes() -> None:
    frame = np.full((PANEL_HEIGHT, PANEL_WIDTH), BLUE, dtype=np.uint8)
    assert set(pack_frame(frame)) == {0x55}
    frame[:] = YELLOW
    assert set(pack_frame(frame)) == {0x22}


@pytest.mark.parametrize(
    "bad",
    [
        np.zeros((PANEL_WIDTH, PANEL_HEIGHT), dtype=np.uint8),  # portrait
        np.zeros((PANEL_HEIGHT, PANEL_WIDTH, 3), dtype=np.uint8),
        np.full((PANEL_HEIGHT, PANEL_WIDTH), 6, dtype=np.uint8),  # index out of range
    ],
)
def test_pack_rejects_bad_frames(bad: np.ndarray) -> None:
    with pytest.raises(ValueError):
        pack_frame(bad)


def test_pack_rejects_bad_rotation() -> None:
    with pytest.raises(ValueError, match="rotation"):
        pack_frame(random_frame(), 180)


def test_unpack_rejects_wrong_length_and_invalid_code() -> None:
    with pytest.raises(ValueError, match="120000"):
        unpack_frame(b"\x11" * 100)
    with pytest.raises(ValueError, match="0x4"):
        unpack_frame(b"\x14" + b"\x11" * (FRAME_BYTES - 1))  # 0x4 is orange: not on this panel


def test_palette_json_round_trip() -> None:
    for pal in (NOMINAL, MEASURED_DEFAULT):
        again = Palette.from_json(pal.to_json())
        assert again == pal
    custom = MEASURED_DEFAULT.with_color(BLUE, (10, 20, 200))
    assert Palette.from_dict("x", custom.as_dict()).rgb == custom.rgb
    assert custom.as_dict()["blue"] == [10, 20, 200]
    assert len(custom.flat()) == 18
    assert custom.as_array().shape == (6, 3)


@pytest.mark.parametrize(
    "rgb",
    [
        ((0, 0, 0),) * 5,  # too few
        ((0, 0, 0),) * 5 + ((0, 0, 256),),  # out of range
        ((0, 0, 0),) * 5 + ((0, 0),),  # not a triple
    ],
)
def test_palette_validation(rgb: tuple) -> None:
    with pytest.raises(ValueError):
        Palette("bad", rgb)


def test_palette_from_dict_missing_colour() -> None:
    colours = NOMINAL.as_dict()
    del colours["green"]
    with pytest.raises(ValueError, match="green"):
        Palette.from_dict("bad", colours)


def test_indices_to_rgb_and_image() -> None:
    idx = np.array([[BLACK, WHITE, YELLOW], [RED, BLUE, GREEN]], dtype=np.uint8)
    rgb = indices_to_rgb(idx, NOMINAL)
    assert rgb.shape == (2, 3, 3) and rgb.dtype == np.uint8
    assert rgb[0, 2].tolist() == [255, 255, 0]
    assert rgb[1, 1].tolist() == [0, 0, 255]
    measured = indices_to_rgb(idx)  # defaults to the measured palette
    assert measured[0, 1].tolist() == list(MEASURED_DEFAULT.rgb[WHITE])
    img = indices_to_image(random_frame(), NOMINAL)
    assert img.mode == "RGB" and img.size == (PANEL_WIDTH, PANEL_HEIGHT)
    with pytest.raises(ValueError):
        indices_to_rgb(np.array([[6]], dtype=np.uint8))


def test_rgb_to_indices_exact() -> None:
    frame = random_frame(3)
    for pal in (NOMINAL, MEASURED_DEFAULT):
        assert np.array_equal(rgb_to_indices_exact(indices_to_rgb(frame, pal), pal), frame)


def test_rgb_to_indices_exact_rejects_off_palette() -> None:
    rgb = indices_to_rgb(random_frame(4), NOMINAL)
    rgb[10, 10] = (128, 128, 128)
    with pytest.raises(ValueError, match="128, 128, 128"):
        rgb_to_indices_exact(rgb, NOMINAL)
    with pytest.raises(ValueError, match="RGB"):
        rgb_to_indices_exact(np.zeros((4, 4), dtype=np.uint8))
