"""Where Inkframe keeps its runtime data, and how it writes files safely.

The data directory is ``$INKFRAME_DATA`` or ``<repo>/data``.  On the Pi it is
``/opt/inkframe/data``, outside the git checkout, so a deploy never touches
photos, the database or settings.  Layout::

    <data>/settings.json      user settings (inkframe.config)
    <data>/inkframe.db        SQLite: photos, app settings, state (inkframe.db)
    <data>/masters/           uploaded photos, downscaled (Phase 3)
    <data>/renders/           dithered render cache (Phase 3)
    <data>/cache/             network app caches (Phase 5)
    <data>/last_frame.png     what the panel shows, rendered in panel colours
    <data>/last_frame.bin     the 120000 controller bytes of that frame
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

#: the package directory (``inkframe/``)
PACKAGE_DIR = Path(__file__).resolve().parent
#: the repository root (the checkout; ``/opt/inkframe/app`` on the Pi)
REPO_ROOT = PACKAGE_DIR.parent
#: no-build frontend served at ``/static``
STATIC_DIR = PACKAGE_DIR / "static"

DATA_ENV_VAR = "INKFRAME_DATA"


def resolve_data_dir(override: Path | str | None = None) -> Path:
    """Return the absolute data directory: ``override``, else ``$INKFRAME_DATA``,
    else ``<repo>/data``.  Does not create it (see ``DataPaths.ensure_dirs``)."""
    if override is not None:
        return Path(override).expanduser().resolve()
    env = os.environ.get(DATA_ENV_VAR, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return REPO_ROOT / "data"


@dataclass(frozen=True)
class DataPaths:
    """Every well-known file and directory under one data root."""

    root: Path

    @property
    def masters(self) -> Path:
        return self.root / "masters"

    @property
    def renders(self) -> Path:
        return self.root / "renders"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    @property
    def db_file(self) -> Path:
        return self.root / "inkframe.db"

    @property
    def last_frame_png(self) -> Path:
        return self.root / "last_frame.png"

    @property
    def last_frame_bin(self) -> Path:
        return self.root / "last_frame.bin"

    def ensure_dirs(self) -> DataPaths:
        """Create the root and its subdirectories if missing; returns ``self``."""
        for d in (self.root, self.masters, self.renders, self.cache):
            d.mkdir(parents=True, exist_ok=True)
        return self


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so readers only ever see the old or the new
    content: write a temp file in the same directory, fsync, ``os.replace``.

    On Windows ``os.replace`` fails while another process holds the target open
    (e.g. a browser download of ``last_frame.png`` on the dev desktop), so the
    rename is retried briefly there.  On the Pi it never needs to.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _replace(src: Path, dst: Path) -> None:
    attempts = 10 if sys.platform == "win32" else 1
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)
