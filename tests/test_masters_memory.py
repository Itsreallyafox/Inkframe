"""Ingest memory, measured: each case runs one ``masters.ingest`` in a fresh
interpreter and compares its real peak with the decode-memory budget and
with ``decode_cost``'s estimate.

* an accepted image must peak at about what the budget allows, and a
  refused one must be refused before its pixels are decoded;
* ``decode_cost`` must be an upper bound of the measured peak.

The peak is the process's peak working set (Windows) or ``VmHWM`` after
``clear_refs`` (Linux, so this also checks the Pi's factors when run there),
taken above the level just before the call, with a 0.5 ms sampler as a
fallback.  Skipped where neither is available.
"""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import sys
import tracemalloc
import zlib
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from inkframe.paths import DataPaths
from inkframe.render import masters
from tests.conftest import encode, jpeg_bytes
from tests.test_masters import _jpeg_with_mpf, _mpf_index, _strip_tiff

REPO_ROOT = Path(__file__).resolve().parent.parent

_PEAK_SCRIPT = r"""
import json, os, sys, tempfile, threading, time, warnings
from pathlib import Path
warnings.simplefilter("ignore")
from inkframe.paths import DataPaths
from inkframe.render import masters

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wt

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("faults", wt.DWORD)] + [
            (n, ctypes.c_size_t)
            for n in ("peak_ws", "ws", "a", "b", "c", "d", "pagefile", "peak_pagefile", "private")
        ]

    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
    handle = k32.GetCurrentProcess()

    def counters():
        c = PMC()
        c.cb = ctypes.sizeof(c)
        k32.K32GetProcessMemoryInfo(handle, ctypes.byref(c), c.cb)
        return c.ws, c.peak_ws

    def reset_peak():
        return True
else:
    page = os.sysconf("SC_PAGE_SIZE")

    def counters():
        rss = hwm = 0
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                elif line.startswith("VmHWM:"):
                    hwm = int(line.split()[1]) * 1024
        return rss, hwm

    def reset_peak():
        try:
            with open("/proc/self/clear_refs", "w") as f:
                f.write("5")
            return True
        except OSError:
            return False

data = Path(sys.argv[1]).read_bytes()
budget = int(sys.argv[2])
masters.heif_available()
paths = DataPaths(Path(tempfile.mkdtemp())).ensure_dirs()
seen = {}
real_check = masters._check_cost

def check(w, h, mode, cost, limit):
    seen["cost"] = cost
    return real_check(w, h, mode, cost, limit)

masters._check_cost = check
exact = reset_peak()
base = counters()[0]
peak = [base]
done = threading.Event()

def sample():
    while not done.is_set():
        peak[0] = max(peak[0], counters()[0])
        time.sleep(0.0005)

t = threading.Thread(target=sample, daemon=True)
t.start()
try:
    masters.ingest(data, "probe", paths=paths, max_decode_bytes=budget)
    status = "ok"
except masters.IngestError as exc:
    status = exc.status
done.set()
t.join()
rss, high = counters()
top = max(peak[0], rss, high if exact else 0)
print(json.dumps({"status": status, "peak": top - base, "cost": seen.get("cost")}))
"""

BUDGET = 60_000_000


def _measure(tmp_path: Path, data: bytes, budget: int = BUDGET) -> tuple[object, int, int]:
    """``(status, peak bytes, estimated bytes)`` of one ingest of ``data``."""
    if sys.platform != "win32" and not Path("/proc/self/status").exists():
        pytest.skip("no way to measure this process's memory here")
    payload = tmp_path / "payload.bin"
    payload.write_bytes(data)
    out = subprocess.run(
        [sys.executable, "-c", _PEAK_SCRIPT, str(payload), str(budget)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=120,
        check=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    return result["status"], int(result["peak"]), int(result["cost"] or 0)


def _textured(width: int, height: int) -> Image.Image:
    """Not flat (so the codecs do real work) but quick to encode."""
    noise = np.random.default_rng(0).integers(0, 256, (48, 72, 3), dtype=np.uint8)
    return Image.fromarray(noise).resize((width, height), Image.Resampling.BILINEAR)


def _smooth(width: int, height: int) -> Image.Image:
    """Gradients and a flat block: compresses well (PNG, deflate TIFF)."""
    x = np.linspace(0, 255, width, dtype=np.float32)[None, :]
    y = np.linspace(0, 255, height, dtype=np.float32)[:, None]
    a = np.empty((height, width, 3), np.uint8)
    a[..., 0], a[..., 1], a[..., 2] = x, y, (x + y) / 2
    a[height // 4 : height // 2, width // 4 : width // 2] = (200, 30, 40)
    return Image.fromarray(a)


def _assert_within_budget(status: object, peak: int, budget: int = BUDGET) -> None:
    if status == "ok":  # accepted: the real peak is roughly what the budget allows
        assert peak <= 1.25 * budget, (
            f"accepted, but the decode peaked at {peak / 1e6:.0f} MB "
            f"for a {budget / 1e6:.0f} MB budget"
        )
    else:  # refused: before the pixels are decoded
        assert peak <= 0.2 * budget, (
            f"refused ({status}) only after decoding: {peak / 1e6:.0f} MB "
            f"for a {budget / 1e6:.0f} MB budget"
        )


def test_harness_control_baseline_jpeg_is_within_budget(tmp_path: Path) -> None:
    # a 12 MP baseline JPEG is drafted to 1/2: well inside a 60 MB budget
    status, peak, _ = _measure(tmp_path, jpeg_bytes(_textured(4000, 3000), quality=80))
    assert status == "ok"
    _assert_within_budget(status, peak)


def test_webp_decode_peak_stays_within_the_budget(tmp_path: Path) -> None:
    # 6 MP: libwebp's two RGBA canvases, the frame's copy and Pillow's image
    # are ~17 bytes a pixel (~100 MB), so a 60 MB budget refuses it up front
    data = encode(_textured(3000, 2000), "WEBP", quality=60)
    status, peak, _ = _measure(tmp_path, data)
    assert status == 422
    _assert_within_budget(status, peak)


def test_progressive_jpeg_decode_peak_stays_within_the_budget(tmp_path: Path) -> None:
    # 17 MP progressive 4:4:4: drafted to 1/2, but libjpeg buffers every
    # full-size coefficient (6 bytes a pixel) for progressive scans
    data = jpeg_bytes(_textured(4800, 3600), quality=85, progressive=True, subsampling=0)
    status, peak, _ = _measure(tmp_path, data)
    assert status == 422
    _assert_within_budget(status, peak)


def test_png_over_budget_is_refused_before_it_is_decoded(tmp_path: Path) -> None:
    # 7.8 MP RGBA: refused, and without decoding it first (getexif() would)
    data = encode(_textured(3000, 2600).convert("RGBA"), "PNG")
    status, peak, _ = _measure(tmp_path, data)
    assert status == 422
    _assert_within_budget(status, peak)


def test_png_text_over_budget_is_refused_before_it_is_read(tmp_path: Path) -> None:
    # 70 zTXt chunks of 1 KB that inflate to 1 MB each: Pillow would hold
    # 70 MB of text inside Image.open, before any check after it could run
    data = _with_text(encode(_smooth(1000, 800), "PNG"), zipped=70, plain=0)
    status, peak, _ = _measure(tmp_path, data)
    assert status == 422
    _assert_within_budget(status, peak)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _with_text(png: bytes, zipped: int, plain: int) -> bytes:
    """``png`` with ``zipped`` zTXt chunks that inflate to 1 MB each before its
    pixels and a ``plain``-byte tEXt chunk after them (read during the load)."""
    bomb = zlib.compress(b"B" * 1_000_000, 9)
    before = b"".join(_chunk(b"zTXt", b"note%02d\x00\x00" % i + bomb) for i in range(zipped))
    after = _chunk(b"tEXt", b"Comment\x00" + b"x" * plain) if plain else b""
    pixels = 8 + 25  # the signature and IHDR
    iend = png.rindex(b"IEND") - 4
    return png[:pixels] + before + png[pixels:iend] + after + png[iend:]


def _mpo() -> bytes:
    im = _textured(4000, 3000)
    return encode(
        im,
        "MPO",
        save_all=True,
        append_images=[im.resize((400, 300))],
        quality=85,
        progressive=True,
    )


def _palette_png() -> bytes:
    indices = np.asarray(_textured(3500, 3400))[..., 0] // 4  # 64 entries
    im = Image.fromarray(indices, "L").convert("P")
    im.putpalette([v for i in range(64) for v in (i * 4, 255 - i * 4, 128)])
    return encode(im, "PNG", compress_level=1, transparency=0)


def _grey16_png() -> bytes:
    grey = np.asarray(_textured(3000, 2600).convert("L"), dtype=np.uint16) * 257
    return encode(Image.fromarray(grey), "PNG", compress_level=1)


def _turned_tiff() -> bytes:
    exif = Image.Exif()
    exif[0x0112] = 6
    return encode(
        _smooth(4000, 3000), "TIFF", compression="tiff_adobe_deflate", exif=exif.tobytes()
    )


def _heic() -> bytes:
    from tests.conftest import heic_bytes

    return heic_bytes(_textured(4032, 3024), quality=80)


def _row_strip_tiff() -> bytes:
    # 100 000 one-row strips (the limit): ~320 bytes of Pillow objects each
    return encode(_textured(24, masters.MAX_TIFF_STRIPS).convert("L"), "TIFF", tiffinfo={278: 1})


ESTIMATED: dict[str, Callable[[], bytes]] = {
    "rgba-png-8mp": lambda: encode(_textured(3000, 2600).convert("RGBA"), "PNG", compress_level=1),
    "palette-png-12mp-trns": _palette_png,
    "grey16-png-8mp": _grey16_png,
    "webp-6mp": lambda: encode(_textured(3000, 2000), "WEBP", quality=60),
    "progressive-jpeg-12mp": lambda: jpeg_bytes(_textured(4000, 3000), progressive=True),
    "progressive-mpo-12mp": _mpo,
    "png-text-8mp": lambda: _with_text(
        encode(_textured(3000, 2600), "PNG", compress_level=1), zipped=20, plain=8_000_000
    ),
    "baseline-jpeg-24mp": lambda: jpeg_bytes(_textured(5712, 4284), quality=85),
    "deflate-tiff-12mp-turned": _turned_tiff,
    "raw-tiff-100k-row-strips": _row_strip_tiff,
    "heic-12mp": _heic,
}


@pytest.mark.parametrize("case", list(ESTIMATED))
def test_decode_cost_is_an_upper_bound(tmp_path: Path, case: str) -> None:
    status, peak, cost = _measure(tmp_path, ESTIMATED[case](), budget=10**12)
    assert status == "ok" and cost > 0
    assert peak <= cost, f"{case}: peaked at {peak / 1e6:.1f} MB, estimated {cost / 1e6:.1f} MB"


# --- EXIF/TIFF IFD tag expansion (folded from the round-3 probe) -----------------------
#
# A crafted ~100 KB file whose embedded EXIF/TIFF IFD holds thousands of tags
# that all point at the same large region makes Pillow materialise
# ``tag_count x region`` bytes (hundreds of MB) when it parses the IFD: inside
# ``Image.open`` for JPEG (``_read_dpi_from_exif`` -> ``getexif()``), in our
# ``_orientation`` for PNG/WEBP, and while the file's own IFD is parsed for
# TIFF.  ``scan_metadata`` now walks the IFD tree on the raw bytes before
# ``Image.open`` and refuses it.  Measured peaks on the desktop (budget 20 MB,
# file size in brackets): before the fix / after it.
#
#   JPEG [66 KB]   +356 MB (accepted!) -> +0.3 MB (422, before Image.open)
#   PNG  [120 KB]  +362 MB (accepted!) -> +0.3 MB (422, before Image.open)
#   WEBP [120 KB]  +364 MB (accepted!) -> +0.2 MB (422, before Image.open)
#   TIFF [120 KB]  +364 MB (OOM on Pi) -> +0.2 MB (422, before Image.open)
#
# A JPEG's MPF segment holds a TIFF-structured MP index that Pillow's
# ``_getmp`` parses inside ``Image.open`` for every JPEG, unpacking every
# value into a Python object (``dict(ifd)``), so it is walked too, its values
# charged as objects:
#
#   MPF, 5450 overlapping tags [66 KB]   +356 MB (accepted!) -> 422, before Image.open
#   MPF, 250 tags of 16 000 LONGs [66 KB] +161 MB (16 MB as raw bytes: accepted!) -> 422
#
# and a TIFF's strips: Pillow builds ~320 bytes of objects per strip at
# ``Image.open``, so a TIFF of 3 million one-row strips (24 MB) peaked at
# +914 MB; more than ``MAX_TIFF_STRIPS`` is refused before ``Image.open``.


def _overlapping_ifd(n_tags: int, size: int) -> bytes:
    """A little-endian TIFF/EXIF IFD whose ``n_tags`` UNDEFINED tags all point
    at the same ``size`` bytes, so ``getexif()`` expands it to ``n_tags * size``
    bytes in memory from an IFD of only ``12 * n_tags`` bytes plus one copy of
    the region.  The leading ``II*\\0`` also makes the block a valid bare TIFF
    file."""
    entries = [(40000 + i, 7, size, 1) for i in range(n_tags)]  # type 7 = UNDEFINED
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


def _webp_with_exif(ifd: bytes) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 10, 10)).save(buf, "WEBP", exif=ifd)
    return buf.getvalue()


IFD_BOMBS: dict[str, Callable[[], bytes]] = {
    "jpeg": lambda: _jpeg_with_exif(_overlapping_ifd(5400, 65_400)),
    "png": lambda: _png_with_exif(_overlapping_ifd(3000, 120_000)),
    "webp": lambda: _webp_with_exif(_overlapping_ifd(3000, 120_000)),
    "tiff": lambda: _overlapping_ifd(3000, 120_000),
    "mpf": lambda: _jpeg_with_mpf(_mpf_index(5450, 7, 65_000)),
    "mpf-longs": lambda: _jpeg_with_mpf(_mpf_index(250, 4, 16_000)),
}


@pytest.mark.parametrize("fmt", list(IFD_BOMBS))
def test_exif_tag_expansion_stays_within_budget(tmp_path: Path, fmt: str) -> None:
    """A ~100 KB upload whose IFD holds thousands of overlapping tags must be
    refused before Pillow parses the IFD, so the peak stays tiny instead of
    ballooning to hundreds of MB (an OOM on the 415 MB Pi)."""
    data = IFD_BOMBS[fmt]()
    assert len(data) < 200_000
    budget = 20_000_000
    paths = DataPaths(tmp_path / f"d-{fmt}").ensure_dirs()
    tracemalloc.start()
    try:
        masters.ingest(data, f"evil.{fmt}", paths=paths, max_decode_bytes=budget)
    except masters.IngestError:
        pass  # a 422 refusal is the fix
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    assert peak < budget * 2, (
        f"{fmt}: ingest peaked at {peak / 1e6:.0f} MB on a {len(data) / 1e3:.0f} KB file"
        f" with a {budget / 1e6:.0f} MB budget"
    )


def test_tiff_strip_flood_stays_within_budget(tmp_path: Path) -> None:
    """A 6 MB TIFF of 3 million one-row strips (one shared row, one shared
    offsets/counts array) is a real TIFF Pillow would open and decode, at
    ~320 bytes of objects per strip (~1 GB). It must be refused from its
    strip count before ``Image.open``, so the peak stays tiny."""
    data = _strip_tiff(3_000_000)
    budget = 20_000_000
    paths = DataPaths(tmp_path / "d").ensure_dirs()
    status: object = "ok"
    tracemalloc.start()
    try:
        masters.ingest(data, "strips.tif", paths=paths, max_decode_bytes=budget)
    except masters.IngestError as exc:
        status = exc.status
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    assert status == 422
    assert peak < budget * 2, (
        f"ingest peaked at {peak / 1e6:.0f} MB on a {len(data) / 1e6:.0f} MB file"
        f" with a {budget / 1e6:.0f} MB budget"
    )
    # and in a fresh interpreter, by the process's own peak
    status, peak, _ = _measure(tmp_path, data, budget)
    assert status == 422 and peak < budget * 2
