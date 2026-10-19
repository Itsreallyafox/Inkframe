"""MockDisplay honours the Display contract and writes honest last-frame files."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from inkframe.display.base import Display, DisplayNotReady
from inkframe.display.mock import MockDisplay, read_last_frame
from inkframe.display.palette import (
    MEASURED_DEFAULT,
    NOMINAL,
    PANEL_HEIGHT,
    PANEL_WIDTH,
    WHITE,
    indices_to_rgb,
    pack_frame,
)
from tests.conftest import stripes_frame


def test_satisfies_protocol(mock_display: MockDisplay) -> None:
    assert isinstance(mock_display, Display)
    assert (mock_display.kind, mock_display.width, mock_display.height) == ("mock", 600, 400)
    assert mock_display.awake is False
    assert mock_display.state == "asleep"


def test_show_before_init_raises(mock_display: MockDisplay, data_dir: Path) -> None:
    with pytest.raises(DisplayNotReady):
        mock_display.show(stripes_frame())
    with pytest.raises(DisplayNotReady):
        mock_display.clear()
    assert not (data_dir / "last_frame.png").exists()
    assert not (data_dir / "last_frame.bin").exists()


def test_show_writes_png_and_bin(mock_display: MockDisplay, data_dir: Path) -> None:
    frame = stripes_frame()
    mock_display.init()
    assert mock_display.awake and mock_display.state == "awake"
    mock_display.show(frame)

    with Image.open(data_dir / "last_frame.png") as img:
        assert img.mode == "RGB"
        assert img.size == (PANEL_WIDTH, PANEL_HEIGHT)
        pixels = np.asarray(img)
    assert np.array_equal(pixels, indices_to_rgb(frame, MEASURED_DEFAULT))

    raw = (data_dir / "last_frame.bin").read_bytes()
    assert len(raw) == 120000
    assert raw == pack_frame(frame, 90)
    assert np.array_equal(read_last_frame(data_dir), frame)

    assert mock_display.show_count == 1
    assert mock_display.last_show_at is not None
    assert mock_display.last_show_at.tzinfo is not None
    assert np.array_equal(mock_display.last_frame, frame)
    assert mock_display.state == "awake"
    # no temp files left behind by the atomic writes
    assert sorted(p.name for p in data_dir.iterdir()) == ["last_frame.bin", "last_frame.png"]


def test_last_frame_is_a_copy(mock_display: MockDisplay) -> None:
    frame = stripes_frame()
    mock_display.init()
    mock_display.show(frame)
    frame[:] = 0
    assert mock_display.last_frame is not None and mock_display.last_frame.max() == 5


def test_palette_and_rotation_callbacks(data_dir: Path) -> None:
    display = MockDisplay(data_dir, palette_fn=lambda: NOMINAL, rotation_fn=lambda: 270)
    frame = stripes_frame()
    display.init()
    display.show(frame)
    with Image.open(data_dir / "last_frame.png") as img:
        assert np.array_equal(np.asarray(img), indices_to_rgb(frame, NOMINAL))
    assert (data_dir / "last_frame.bin").read_bytes() == pack_frame(frame, 270)
    assert np.array_equal(read_last_frame(data_dir, 270), frame)


def test_clear_writes_white(mock_display: MockDisplay, data_dir: Path) -> None:
    mock_display.init()
    mock_display.show(stripes_frame())
    mock_display.clear()
    white = np.full((PANEL_HEIGHT, PANEL_WIDTH), WHITE, dtype=np.uint8)
    assert np.array_equal(read_last_frame(data_dir), white)
    with Image.open(data_dir / "last_frame.png") as img:
        assert set(img.getcolors()) == {(PANEL_WIDTH * PANEL_HEIGHT, MEASURED_DEFAULT.rgb[WHITE])}
    assert mock_display.clear_count == 1 and mock_display.show_count == 1
    assert np.array_equal(mock_display.last_frame, white)


def test_sleep_is_idempotent_and_blocks_show(mock_display: MockDisplay) -> None:
    mock_display.sleep()  # never initialised: still fine
    mock_display.init()
    mock_display.sleep()
    mock_display.sleep()
    assert mock_display.awake is False and mock_display.state == "asleep"
    with pytest.raises(DisplayNotReady):
        mock_display.show(stripes_frame())
    mock_display.init()
    mock_display.show(stripes_frame())
    assert mock_display.show_count == 1


@pytest.mark.parametrize(
    "bad",
    [
        np.zeros((PANEL_WIDTH, PANEL_HEIGHT), dtype=np.uint8),
        np.zeros((PANEL_HEIGHT, PANEL_WIDTH, 3), dtype=np.uint8),
        np.full((PANEL_HEIGHT, PANEL_WIDTH), 7, dtype=np.uint8),
        np.zeros((PANEL_HEIGHT, PANEL_WIDTH), dtype=np.float32),
    ],
)
def test_bad_frame_raises_and_writes_nothing(
    mock_display: MockDisplay, data_dir: Path, bad: np.ndarray
) -> None:
    mock_display.init()
    with pytest.raises(ValueError):
        mock_display.show(bad)
    assert list(data_dir.iterdir()) == []
    assert mock_display.show_count == 0 and mock_display.last_frame is None


def test_refresh_seconds_blocks(data_dir: Path) -> None:
    display = MockDisplay(data_dir, refresh_seconds=0.2)
    display.init()
    started = time.monotonic()
    display.show(stripes_frame())
    assert time.monotonic() - started >= 0.2


def test_logs_each_operation(mock_display: MockDisplay, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="inkframe.display.mock"):
        mock_display.init()
        mock_display.show(stripes_frame())
        mock_display.clear()
        mock_display.sleep()
    messages = [r.getMessage() for r in caplog.records if r.name == "inkframe.display.mock"]
    assert len(messages) == 4
    assert [m.split(":")[0] for m in messages] == [
        "mock init",
        "mock show",
        "mock clear",
        "mock sleep",
    ]
