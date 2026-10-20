"""Shared fixtures: every test gets its own throw-away data directory.

Also synthetic test images (generated in code, never checked in) and a
``photo_factory`` that ingests one into a fresh database."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from inkframe.app import create_app
from inkframe.config import SettingsStore
from inkframe.db import Database, Photo
from inkframe.display.mock import MockDisplay
from inkframe.display.palette import MEASURED_DEFAULT_RGB, NOMINAL_RGB, PANEL_HEIGHT, PANEL_WIDTH
from inkframe.paths import DataPaths


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


# --- synthetic images --------------------------------------------------------------


def gradient_image(width: int = 2400, height: int = 1600) -> Image.Image:
    """A photo stand-in: a hue sweep left to right, dark to light top to bottom,
    a grey ramp along the bottom eighth, and twelve flat patches (the nominal
    and measured palette colours) so every dither method has edges, flats and
    tones to work on."""
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    u, v = x / max(1, width - 1), y / max(1, height - 1)
    rgb = np.stack(
        [
            0.5 + 0.5 * np.cos(2 * np.pi * (u - 0.0)),
            0.5 + 0.5 * np.cos(2 * np.pi * (u - 1 / 3)),
            0.5 + 0.5 * np.cos(2 * np.pi * (u - 2 / 3)),
        ],
        axis=-1,
    )
    rgb = rgb * (0.15 + 0.85 * v)[..., None]
    a = np.clip(np.rint(rgb * 255), 0, 255).astype(np.uint8)
    ramp_top = height - height // 8
    a[ramp_top:, :, :] = np.rint(u[ramp_top:, :, None] * 255).astype(np.uint8)
    pw, ph = width // 16, height // 10
    for i, colour in enumerate(NOMINAL_RGB + MEASURED_DEFAULT_RGB):
        x0 = (i % 6) * (width // 6) + pw // 2
        y0 = ph + (i // 6) * 2 * ph
        a[y0 : y0 + ph, x0 : x0 + pw] = colour
    return Image.fromarray(a)


def encode(image: Image.Image, fmt: str, **kw: Any) -> bytes:
    """``image`` saved as ``fmt`` (``"JPEG"``, ``"PNG"``, ``"WEBP"``, ``"HEIF"``...)."""
    buf = io.BytesIO()
    image.save(buf, fmt, **kw)
    return buf.getvalue()


def jpeg_bytes(image: Image.Image | None = None, quality: int = 90, **kw: Any) -> bytes:
    """A JPEG of ``image`` (default: the 2400 x 1600 ``gradient_image``)."""
    return encode(image if image is not None else gradient_image(), "JPEG", quality=quality, **kw)


def heic_bytes(image: Image.Image, quality: int = 85, **kw: Any) -> bytes:
    """``image`` as HEIC through pillow-heif (registering its Pillow plugin);
    skips the calling test if this platform has no HEVC encoder."""
    pillow_heif = pytest.importorskip("pillow_heif")
    if not pillow_heif.libheif_info().get("encoders"):
        pytest.skip("pillow-heif has no HEVC encoder on this platform")
    from inkframe.render import masters

    assert masters.heif_available()
    return encode(image, "HEIF", quality=quality, **kw)


# --- photos ---------------------------------------------------------------------------


@pytest.fixture
def data_paths(data_dir: Path) -> DataPaths:
    """``DataPaths`` of ``data_dir`` with its subdirectories created."""
    return DataPaths(data_dir).ensure_dirs()


@pytest.fixture
def database(data_paths: DataPaths) -> Iterator[Database]:
    """A fresh database in ``data_dir``."""
    db = Database(data_paths.db_file)
    yield db
    db.close()


PhotoFactory = Callable[..., Photo]


@pytest.fixture
def photo_factory(database: Database, data_paths: DataPaths) -> PhotoFactory:
    """``make(image=None, *, name="photo.jpg", recipe=None, data=None) -> Photo``:
    ingest an image (default ``gradient_image()`` as JPEG; ``data`` passes
    encoded bytes straight through) into ``database`` and ``data_paths``."""
    from inkframe.render import masters

    def make(
        image: Image.Image | None = None,
        *,
        name: str = "photo.jpg",
        recipe: Mapping[str, Any] | None = None,
        data: bytes | None = None,
    ) -> Photo:
        payload = data if data is not None else jpeg_bytes(image)
        master = masters.ingest(payload, name, paths=data_paths)
        return database.add_photo(
            photo_id=master.id,
            original_name=master.original_name,
            width=master.width,
            height=master.height,
            portrait=master.portrait,
            master_path=master.relative_path,
            master_bytes=master.bytes,
            recipe=recipe,
        )

    return make
