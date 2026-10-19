"""Shared fixtures: every test gets its own throw-away data directory."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from inkframe.app import create_app
from inkframe.config import SettingsStore
from inkframe.display.mock import MockDisplay
from inkframe.display.palette import PANEL_HEIGHT, PANEL_WIDTH


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the developer's INKFRAME_* environment leak into a test."""
    for name in ("INKFRAME_DATA", "INKFRAME_DISPLAY", "INKFRAME_HOST", "INKFRAME_PORT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def settings_store(data_dir: Path) -> SettingsStore:
    return SettingsStore(data_dir / "settings.json")


@pytest.fixture
def mock_display(data_dir: Path) -> MockDisplay:
    return MockDisplay(data_dir)


@pytest.fixture
def client(data_dir: Path, mock_display: MockDisplay) -> Iterator[TestClient]:
    """A running app (lifespan entered) on ``data_dir`` with ``mock_display``."""
    app = create_app(data_dir=data_dir, display=mock_display)
    with TestClient(app) as c:
        yield c


def stripes_frame() -> np.ndarray:
    """A (400, 600) frame cycling through all six indices in 100-px columns."""
    cols = (np.arange(PANEL_WIDTH) // 100) % 6
    return np.tile(cols.astype(np.uint8), (PANEL_HEIGHT, 1))
