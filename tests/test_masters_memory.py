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

import json
import os
import struct
import subprocess
import sys
import zlib
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from tests.conftest import encode, jpeg_bytes

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
    "heic-12mp": _heic,
}


@pytest.mark.parametrize("case", list(ESTIMATED))
def test_decode_cost_is_an_upper_bound(tmp_path: Path, case: str) -> None:
    status, peak, cost = _measure(tmp_path, ESTIMATED[case](), budget=10**12)
    assert status == "ok" and cost > 0
    assert peak <= cost, f"{case}: peaked at {peak / 1e6:.1f} MB, estimated {cost / 1e6:.1f} MB"
