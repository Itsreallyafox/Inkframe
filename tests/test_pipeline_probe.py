"""Adversarial probes kept for the fixer (round 3).

Each test here demonstrates a concrete hole.  They are written to FAIL while
the hole is open; delete or invert them once it is closed.
"""

from __future__ import annotations

import io
import struct
import tracemalloc
import zlib
from pathlib import Path

import pytest
from PIL import Image

from inkframe.paths import DataPaths
from inkframe.render import masters


def _overlapping_ifd(n_tags: int, size: int) -> bytes:
    """A little-endian TIFF/EXIF IFD whose ``n_tags`` UNDEFINED tags all point
    at the same ``size`` bytes.  Pillow's ``getexif()._get_merged_dict()``
    materialises each tag's value, so this expands to ``n_tags * size`` bytes
    in memory from an IFD of only ``12 * n_tags`` bytes plus one copy of the
    region: the file stays tiny, the peak does not."""
    entries = [(40000 + i, 7, size, 1) for i in range(n_tags)]  # type 7 = UNDEFINED, offset 1
    ifd = (
        struct.pack("<H", len(entries))
        + b"".join(struct.pack("<HHII", t, typ, c, v) for t, typ, c, v in entries)
        + b"\x00\x00\x00\x00"
    )
    blob = b"II*\x00" + struct.pack("<I", 8) + ifd
    return blob + b"\x00" * max(0, size + 1 - len(blob))


def _png_with_exif(ifd: bytes) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 10, 10)).save(buf, "PNG")
    png = buf.getvalue()
    chunk = (
        struct.pack(">I", len(ifd)) + b"eXIf" + ifd + struct.pack(">I", zlib.crc32(b"eXIf" + ifd))
    )
    return png[:33] + chunk + png[33:]


def _jpeg_with_exif(ifd: bytes) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 10, 10)).save(buf, "JPEG")
    jpeg = buf.getvalue()
    payload = b"Exif\x00\x00" + ifd
    return jpeg[:2] + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + jpeg[2:]


@pytest.mark.parametrize(
    ("label", "data"),
    [
        ("png", _png_with_exif(_overlapping_ifd(3000, 120_000))),
        ("jpeg", _jpeg_with_exif(_overlapping_ifd(5400, 65_400))),
    ],
    ids=["png", "jpeg"],
)
def test_exif_tag_expansion_blows_the_decode_budget(
    tmp_path: Path, label: str, data: bytes
) -> None:
    """HOLE: a ~100 KB upload makes Pillow materialise hundreds of MB of EXIF
    tag values while reading orientation (PNG/WEBP) or even inside
    ``Image.open`` (JPEG, via ``_read_dpi_from_exif``), far over
    ``max_decode_bytes``.  ``scan_metadata`` only counts the raw segment/chunk
    bytes, not the per-tag expansion of overlapping IFD entries, so the budget
    check never prevents it.  On the 415 MB Pi this is an OOM."""
    assert len(data) < 200_000
    budget = 20_000_000
    paths = DataPaths(tmp_path / f"d-{label}").ensure_dirs()
    tracemalloc.start()
    try:
        masters.ingest(data, f"evil.{label}", paths=paths, max_decode_bytes=budget)
    except masters.IngestError:
        pass  # a 422 refusal is the fix; the peak below is what must shrink
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    # the hole: peak dwarfs the budget despite the tiny file
    assert peak < budget * 2, (
        f"{label}: ingest peaked at {peak / 1e6:.0f} MB on a {len(data) / 1e3:.0f} KB file"
        f" with a {budget / 1e6:.0f} MB budget"
    )
