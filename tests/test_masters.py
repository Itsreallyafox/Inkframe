"""Upload ingest: sniffing, limits, decoding, orientation, alpha, masters on disk."""

from __future__ import annotations

import io
import struct
import time
import tracemalloc
import warnings
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import BmpImagePlugin, Image, ImageCms, ImageOps, PngImagePlugin

from inkframe.paths import DataPaths
from inkframe.render import masters
from inkframe.render.masters import (
    MASTER_LONG_EDGE,
    UndecodableImage,
    UnsupportedImageType,
    UploadTooLarge,
    ingest,
    sanitize_name,
    sniff_format,
)
from tests.conftest import encode, gradient_image, heic_bytes, jpeg_bytes


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    return DataPaths(tmp_path / "data").ensure_dirs()


def _open_master(m: masters.Master) -> Image.Image:
    im = Image.open(m.path)
    im.load()
    return im


def _png_header_only(width: int, height: int) -> bytes:
    """A PNG that declares ``width`` x ``height`` but carries one byte of
    pixel data: what a decompression bomb looks like to ``Image.open``."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00"))
        + chunk(b"IEND", b"")
    )


# --- sniffing ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "expected", "kw"),
    [
        ("JPEG", "jpeg", {}),
        ("PNG", "png", {}),
        ("WEBP", "webp", {}),
        ("TIFF", "tiff", {}),
        ("BMP", "bmp", {}),
        ("GIF", "gif", {}),
    ],
)
def test_sniff_recognises_formats(fmt: str, expected: str, kw: dict) -> None:
    data = encode(Image.new("RGB", (32, 24), (10, 120, 200)), fmt, **kw)
    assert sniff_format(data) == expected


def test_sniff_heic_and_heif_brands() -> None:
    def ftyp(major: bytes, *compat: bytes) -> bytes:
        body = major + b"\x00\x00\x00\x00" + b"".join(compat)
        return struct.pack(">I", 8 + len(body)) + b"ftyp" + body + b"\x00" * 16

    assert sniff_format(ftyp(b"heic", b"mif1", b"heic")) == "heif"
    assert sniff_format(ftyp(b"mif1", b"heix")) == "heif"
    assert sniff_format(ftyp(b"msf1", b"hevc")) == "heif"
    assert sniff_format(ftyp(b"mif1", b"miaf")) == "heif"
    with pytest.raises(UnsupportedImageType, match="AVIF"):
        sniff_format(ftyp(b"avif", b"mif1", b"miaf"))
    with pytest.raises(UnsupportedImageType):
        sniff_format(ftyp(b"isom", b"mp41"))  # an MP4 video


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"hello, this is a text file pretending to be a jpg\n",
        b"%PDF-1.7\n",
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        b"\xff\xd8",  # too short to be a JPEG
        b"BMW service history, 2019-2026\n" * 4,  # "BM" but no DIB header
        b"MM\x00 not quite a TIFF",
    ],
)
def test_sniff_rejects_non_images(data: bytes) -> None:
    with pytest.raises(UnsupportedImageType):
        sniff_format(data)


def test_text_file_named_jpg_is_unsupported(paths: DataPaths) -> None:
    with pytest.raises(UnsupportedImageType) as exc:
        ingest(b"just some text\n" * 100, "holiday.jpg", paths=paths)
    assert exc.value.status == 415
    assert list(paths.masters.iterdir()) == []


# --- limits -------------------------------------------------------------------------------


def test_30_mb_file_is_too_large(paths: DataPaths) -> None:
    data = b"\xff\xd8\xff" + bytes(30 * 1024 * 1024)
    with pytest.raises(UploadTooLarge) as exc:
        ingest(data, "big.jpg", paths=paths)
    assert exc.value.status == 413 and "25 MB" in str(exc.value)
    assert list(paths.masters.iterdir()) == []


def test_size_limit_is_inclusive(paths: DataPaths) -> None:
    data = jpeg_bytes(Image.new("RGB", (60, 40), (1, 2, 3)))
    assert ingest(data, "a.jpg", paths=paths, max_bytes=len(data)).width == 60
    with pytest.raises(UploadTooLarge):
        ingest(data, "a.jpg", paths=paths, max_bytes=len(data) - 1)


def test_decompression_bomb_is_refused_before_decoding(paths: DataPaths) -> None:
    # 400 MP: more than twice the limit, Pillow itself raises at open
    with pytest.raises(UndecodableImage, match="decompression bomb") as exc:
        ingest(_png_header_only(20000, 20000), "bomb.png", paths=paths)
    assert exc.value.status == 422
    # 63 MP: over the limit but under Pillow's, refused by our own check
    with pytest.raises(UndecodableImage, match="63.0 MP"):
        ingest(_png_header_only(9000, 7000), "bomb.png", paths=paths)
    assert Image.MAX_IMAGE_PIXELS != masters.MAX_PIXELS


def test_pillows_pixel_limit_is_never_changed(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Image.MAX_IMAGE_PIXELS is process-wide: another thread opening an image
    # during an ingest must not see a different value
    seen: list[int | None] = []
    real_open = Image.open

    def spy(*args: Any, **kw: Any) -> Image.Image:
        seen.append(Image.MAX_IMAGE_PIXELS)
        return real_open(*args, **kw)

    default = Image.MAX_IMAGE_PIXELS
    monkeypatch.setattr(Image, "open", spy)
    ingest(encode(Image.new("RGB", (60, 40)), "PNG"), "a.png", paths=paths)
    with pytest.raises(UndecodableImage, match="63.0 MP"):
        ingest(_png_header_only(9000, 7000), "big.png", paths=paths)
    assert len(seen) == 2 and set(seen) == {default}


def test_decode_memory_guard(paths: DataPaths) -> None:
    data = encode(Image.new("RGBA", (2000, 1500), (0, 0, 0, 0)), "PNG")
    with pytest.raises(UndecodableImage, match="MB") as exc:
        ingest(data, "a.png", paths=paths, max_decode_bytes=10_000_000)
    assert exc.value.status == 422 and "2000 x 1500" in str(exc.value)
    assert list(paths.masters.iterdir()) == []


def test_png_budget_check_runs_before_load(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No PNG pixel is decoded for an image the budget refuses
    (``PngImageFile.getexif()`` would decode it: the orientation is read
    only after the check)."""
    loads: list[tuple[int, int]] = []
    real = PngImagePlugin.PngImageFile.load

    def spy(self: PngImagePlugin.PngImageFile) -> object:
        loads.append(self.size)
        return real(self)

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", spy)
    data = encode(Image.new("RGBA", (2000, 1500), (0, 0, 0, 0)), "PNG")
    with pytest.raises(UndecodableImage, match="MB"):
        ingest(data, "a.png", paths=paths, max_decode_bytes=10_000_000)
    assert loads == [], "the PNG was decoded before the budget refused it"
    # accepted: decoded once, and an eXIf chunk after the pixels still counts
    exif = Image.Exif()
    exif[0x0112] = 6
    trailing = _exif_after_idat(encode(Image.new("RGB", (60, 40)), "PNG", exif=exif.tobytes()))
    m = ingest(trailing, "late-exif.png", paths=paths)
    assert set(loads) == {(60, 40)} and (m.width, m.height) == (40, 60) and m.portrait


def _exif_after_idat(png: bytes) -> bytes:
    """``png`` with its eXIf chunk moved after the pixel data (legal, and what
    makes ``PngImageFile.getexif()`` decode the whole image)."""
    chunks, pos = [], 8
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        chunks.append(png[pos : pos + 12 + length])
        pos += 12 + length
    exif = [c for c in chunks if c[4:8] == b"eXIf"]
    rest = [c for c in chunks if c[4:8] != b"eXIf"]
    assert len(exif) == 1 and rest[-1][4:8] == b"IEND"
    return png[:8] + b"".join(rest[:-1] + exif + rest[-1:])


def test_decode_cost_model() -> None:
    cost = masters.decode_cost
    # never less than the image Pillow decodes into, plus the margin
    for mode, per in (("L", 1), ("P", 1), ("I;16", 2), ("RGB", 4), ("RGBA", 4), ("LA", 4)):
        assert cost(mode, (4000, 3000), "png") > 4000 * 3000 * per * masters._COST_MARGIN
    # converting modes build the reduced copy strip by strip: a palette PNG is
    # far cheaper than its RGBA expansion would be
    assert cost("P", (8000, 6000), "png", transparency=True) < 8000 * 6000 * 2
    # WebP: libwebp's two RGBA canvases and the frame's copy, ~17 bytes a pixel
    assert cost("RGB", (3000, 2000), "webp") >= 3000 * 2000 * 17 * masters._COST_MARGIN
    # decoder_bytes (a progressive JPEG's coefficients...) is added to the decode
    held = 2856 * 2142 * 4 + 50_000_000
    assert cost("RGB", (2856, 2142), "jpeg", decoder_bytes=50_000_000) > held * masters._COST_MARGIN
    # HEIF: libheif's planes beside its RGB buffer
    assert cost("RGB", (5712, 4284), "heif") > 5712 * 4284 * 4.5
    # the default budget, the upload's bytes included: a 24 MP iPhone HEIC
    # (measured +120 MB) fits; a 48 MP "HEIF Max" one (+235 MB), a 30 MP RGBA
    # PNG, a 12 MP WebP and a 48 MP progressive JPEG do not
    budget = masters.MAX_DECODE_BYTES
    assert cost("RGB", (5712, 4284), "heif") + 3_000_000 <= budget
    assert cost("RGB", (8064, 6048), "heif") > budget
    assert cost("RGBA", (6700, 4470), "png") > budget
    assert cost("RGB", (4000, 3000), "webp") > budget
    progressive_48 = masters._jpeg_coefficient_bytes(
        (8000, 6000), [(1, 2, 2, 0), (2, 1, 1, 1), (3, 1, 1, 1)]
    )
    assert cost("RGB", (2000, 1500), "jpeg", decoder_bytes=progressive_48) > budget
    # while every baseline phone JPEG is drafted and cheap
    assert cost("RGB", (2016, 1512), "jpeg") < 60_000_000


def test_jpeg_coefficient_buffer() -> None:
    # 64 two-byte coefficients per 8 x 8 block of each component: 6 bytes a
    # pixel at 4:4:4, 3 at 4:2:0, whatever the draft
    full = [(1, 1, 1, 0), (2, 1, 1, 1), (3, 1, 1, 1)]
    sub = [(1, 2, 2, 0), (2, 1, 1, 1), (3, 1, 1, 1)]
    assert masters._jpeg_coefficient_bytes((4800, 3600), full) == 4800 * 3600 * 6
    assert masters._jpeg_coefficient_bytes((4800, 3600), sub) == 4800 * 3600 * 3
    assert masters._jpeg_coefficient_bytes((4801, 3601), sub) > 4801 * 3601 * 3  # MCU padding
    img = Image.new("RGB", (64, 48), (10, 20, 30))
    for kw, buffered in (({}, False), ({"progressive": True}, True)):
        data = jpeg_bytes(img, **kw)
        assert masters._jpeg_buffers_coefficients(data) is buffered
        with Image.open(io.BytesIO(data)) as im:
            assert (masters.decoder_overhead(im, data) > 0) is buffered


def _jpeg_headers(frame_components: int, scan_components: int, sof: int = 0xC0) -> bytes:
    def seg(marker: int, payload: bytes) -> bytes:
        return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload

    sof_payload = struct.pack(">BHHB", 8, 48, 64, frame_components) + b"\x01\x11\x00" * (
        frame_components
    )
    sos_payload = bytes([scan_components]) + b"\x01\x00" * scan_components + b"\x00\x3f\x00"
    return (
        b"\xff\xd8"
        + seg(0xE1, b"Exif\x00\x00" + b"\xff\xda" * 8)  # an SOS inside APP1 is skipped
        + seg(sof, sof_payload)
        + seg(0xDA, sos_payload)
    )


def test_jpeg_scan_layout_from_headers() -> None:
    # a sequential frame whose first scan carries every component is decoded
    # in one pass; a non-interleaved one (more scans follow), a progressive
    # or a lossless frame makes libjpeg buffer every coefficient
    assert masters._jpeg_buffers_coefficients(_jpeg_headers(3, 3)) is False
    assert masters._jpeg_buffers_coefficients(_jpeg_headers(1, 1)) is False
    assert masters._jpeg_buffers_coefficients(_jpeg_headers(3, 1)) is True
    assert masters._jpeg_buffers_coefficients(_jpeg_headers(3, 3, sof=0xC2)) is True
    assert masters._jpeg_buffers_coefficients(_jpeg_headers(3, 3, sof=0xC3)) is True
    assert masters._jpeg_buffers_coefficients(b"\xff\xd8\x00garbage") is True


def test_tiff_decoder_overhead() -> None:
    img = Image.new("RGB", (400, 300), (1, 2, 3))
    exif = Image.Exif()
    exif[0x0112] = 6
    plain = encode(img, "TIFF")
    turned = encode(img, "TIFF", exif=exif.tobytes())
    one_strip = encode(img, "TIFF", compression="tiff_adobe_deflate", strip_size=1 << 30)
    with Image.open(io.BytesIO(plain)) as im:
        assert masters.decoder_overhead(im, plain) == 0  # raw strips: no libtiff buffer
    with Image.open(io.BytesIO(turned)) as im:
        # Pillow transposes the loaded image in place: a second full-size image
        assert masters.decoder_overhead(im, turned) == 400 * 300 * 4
    with Image.open(io.BytesIO(one_strip)) as im:
        assert masters.decoder_overhead(im, one_strip) == 400 * 300 * 4  # the whole-image strip


@pytest.mark.parametrize(("mode", "factor"), [("RGB", 3), ("RGBA", 2), ("L", 4), ("RGB", 1)])
def test_reduce_rows_matches_a_whole_image_reduce(
    monkeypatch: pytest.MonkeyPatch, mode: str, factor: int
) -> None:
    w, h, bands = 301, 257, len(mode)
    stride = w * bands + 7  # padded rows, like libheif's
    rng = np.random.default_rng(3)
    raw = rng.integers(0, 256, h * stride, dtype=np.uint8).tobytes()
    monkeypatch.setattr(masters, "_STRIP_BYTES", 5 * stride)  # many strips
    got = masters.reduce_rows(raw, mode, (w, h), stride, factor)
    want = Image.frombytes(mode, (w, h), raw, "raw", mode, stride)
    if factor > 1:
        want = want.reduce(factor)
    assert got.mode == mode and got.size == want.size
    assert np.array_equal(np.asarray(got), np.asarray(want))


def test_truncated_jpeg_is_undecodable(paths: DataPaths) -> None:
    data = jpeg_bytes()
    with pytest.raises(UndecodableImage) as exc:
        ingest(data[: len(data) // 2], "cut.jpg", paths=paths)
    assert exc.value.status == 422
    assert list(paths.masters.iterdir()) == []


def test_garbage_after_valid_magic_is_undecodable(paths: DataPaths) -> None:
    with pytest.raises(UndecodableImage):
        ingest(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200, "x.png", paths=paths)
    with pytest.raises(UndecodableImage):
        ingest(b"\xff\xd8\xff\xe0" + b"\x13" * 500, "x.jpg", paths=paths)


# --- decoding and geometry --------------------------------------------------------------------


def test_large_jpeg_becomes_1800_wide_master(paths: DataPaths) -> None:
    m = ingest(jpeg_bytes(), "gradient.jpg", paths=paths)
    assert (m.width, m.height) == (1800, 1200) and m.portrait is False
    assert m.path == paths.masters / f"{m.id}.jpg" and m.path.is_file()
    assert m.relative_path == f"masters/{m.id}.jpg"
    assert m.bytes == m.path.stat().st_size
    assert m.source_format == "jpeg"
    im = _open_master(m)
    assert im.format == "JPEG" and im.mode == "RGB" and im.size == (1800, 1200)
    assert "exif" not in im.info and "icc_profile" not in im.info
    assert "progressive" not in im.info and "progression" not in im.info
    # the patches survive: the nominal red patch at its scaled position
    x = round((3 * (2400 // 6) + 2400 // 32 + 10) * 0.75)
    y = round((1600 // 10 + 20) * 0.75)
    r, g, b = im.getpixel((x, y))
    assert r > 200 and g < 50 and b < 50


def test_small_images_are_never_upscaled(paths: DataPaths) -> None:
    m = ingest(jpeg_bytes(Image.new("RGB", (320, 200), (50, 60, 70))), "s.jpg", paths=paths)
    assert (m.width, m.height) == (320, 200)


def test_portrait_jpeg(paths: DataPaths) -> None:
    img = gradient_image(800, 1200)
    m = ingest(jpeg_bytes(img), "portrait.jpg", paths=paths)
    assert (m.width, m.height) == (800, 1200) and m.portrait is True


def test_exif_orientation_is_applied(paths: DataPaths) -> None:
    a = np.full((400, 600, 3), 200, np.uint8)
    a[:80, :120] = (255, 0, 0)  # a red block in the stored image's top-left corner
    exif = Image.Exif()
    exif[0x0112] = 6  # "rotate 90 CW to display"
    data = jpeg_bytes(Image.fromarray(a), quality=95, exif=exif.tobytes())
    m = ingest(data, "rotated.jpg", paths=paths)
    assert (m.width, m.height) == (400, 600) and m.portrait is True
    im = _open_master(m)
    assert "exif" not in im.info  # orientation applied, metadata dropped
    top_right = im.getpixel((400 - 20, 20))
    top_left = im.getpixel((20, 20))
    assert top_right[0] > 200 and top_right[1] < 60  # red moved to the top right
    assert top_left[0] < 230 and abs(top_left[0] - top_left[1]) < 30  # grey stays grey


#: every format whose EXIF orientation Inkframe applies (HEIF: libheif does)
_ORIENTED = {
    "PNG": {},
    "JPEG": {"quality": 95, "subsampling": 0},
    "WEBP": {"lossless": True},
    "TIFF": {},  # Pillow turns a TIFF itself while loading it: exactly once in all
}


@pytest.mark.parametrize("orientation", [1, 2, 3, 4, 5, 6, 7, 8])
@pytest.mark.parametrize("fmt", list(_ORIENTED))
def test_every_orientation_matches_exif_transpose(
    paths: DataPaths, fmt: str, orientation: int
) -> None:
    a = np.full((40, 60, 3), 128, np.uint8)
    a[:10, :20] = (255, 0, 0)  # red in the stored top-left corner
    a[-10:, -6:] = (0, 0, 255)  # blue in the stored bottom-right one
    exif = Image.Exif()
    exif[0x0112] = orientation
    data = encode(Image.fromarray(a), fmt, exif=exif.tobytes(), **_ORIENTED[fmt])
    m = ingest(data, f"o.{fmt.lower()}", paths=paths)
    expected = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    assert (m.width, m.height) == expected.size
    assert m.portrait is (expected.size[1] > expected.size[0])
    got = np.asarray(_open_master(m), dtype=np.int16)
    want = np.asarray(expected, dtype=np.int16)
    assert np.abs(got - want).mean() < 6  # identical up to JPEG noise


@pytest.mark.parametrize("orientation", [2, 3, 6, 8])
def test_tiff_orientation_is_applied_exactly_once(paths: DataPaths, orientation: int) -> None:
    # Pillow's TIFF plugin runs exif_transpose in load_end and reports the
    # turned size at open; turning it again would undo or double it
    a = np.full((30, 60, 3), 128, np.uint8)
    a[:10, :15] = (255, 0, 0)
    exif = Image.Exif()
    exif[0x0112] = orientation
    for kw in ({}, {"compression": "tiff_adobe_deflate"}):  # raw and libtiff decoders
        data = encode(Image.fromarray(a), "TIFF", exif=exif.tobytes(), **kw)
        m = ingest(data, "o.tif", paths=paths)
        want = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        assert (m.width, m.height) == want.size
        assert m.portrait is (want.size[1] > want.size[0])
        got = np.asarray(_open_master(m), dtype=np.int16)
        assert np.abs(got - np.asarray(want, dtype=np.int16)).mean() < 6


def test_png_alpha_is_composited_on_white(paths: DataPaths) -> None:
    a = np.zeros((300, 400, 4), np.uint8)
    a[:, 200:] = (220, 20, 30, 255)  # opaque red right half
    a[:, :200] = (0, 0, 0, 0)  # transparent (black) left half
    m = ingest(encode(Image.fromarray(a), "PNG"), "alpha.png", paths=paths)
    im = _open_master(m)
    assert im.mode == "RGB"
    left = im.getpixel((50, 150))
    right = im.getpixel((350, 150))
    assert min(left) >= 250  # white, not black
    assert right[0] > 200 and right[1] < 50


def test_large_rgba_png_reduces_without_dark_fringes(paths: DataPaths) -> None:
    # 7400 x 2000 is box-reduced 4x before the Lanczos step.  The edge at
    # x = 3702 splits a 4-pixel block between transparent black and opaque
    # green: averaged without premultiplying, that block would come out a
    # dark grey-green; composited properly it is half green, half white, and
    # every pixel of the master lies on the line from white to the green
    green = np.array([40, 160, 60])
    im = Image.new("RGBA", (7400, 2000), (0, 0, 0, 0))
    im.paste((*green.tolist(), 255), (3702, 0, 7400, 2000))
    data = encode(im, "PNG")
    master = masters.decode_master(data, "png")  # before JPEG noise
    assert master.size == (1800, 486)
    arr = np.asarray(master, dtype=np.float64)
    assert arr[:, :880].min() >= 254  # white where it was transparent
    assert np.abs(arr[:, 920:] - green).max() <= 1  # the green, untouched
    t = (255 - arr) / (255 - green)  # how far towards the green, per channel
    assert np.abs(t[..., 0] - t[..., 1]).max() < 0.05
    assert np.abs(t[..., 2] - t[..., 1]).max() < 0.05
    edge = t[:, 895:906, 1]
    assert ((edge > 0.2) & (edge < 0.8)).any()  # the half-covered block is there
    m = ingest(data, "huge.png", paths=paths)
    assert (m.width, m.height) == (1800, 486)


@pytest.mark.parametrize("mode", ["RGB", "L"])
def test_png_trns_colour_key_is_composited_on_white(paths: DataPaths, mode: str) -> None:
    a = np.full((40, 60, 3), 200, np.uint8)
    a[:, :30] = 0  # the left half is the transparent key colour
    im = Image.fromarray(a).convert(mode)
    data = encode(im, "PNG", transparency=(0, 0, 0) if mode == "RGB" else 0)
    m = ingest(data, "key.png", paths=paths)
    out = _open_master(m).convert("RGB")
    assert min(out.getpixel((10, 20))) >= 245, "transparent pixels must be white"
    assert all(abs(c - 200) <= 4 for c in out.getpixel((50, 20)))


def test_16_bit_png_trns_colour_key(paths: DataPaths) -> None:
    a = np.full((40, 60), 40000, np.uint16)
    a[:, :30] = 1234  # the 16-bit key
    data = encode(Image.fromarray(a), "PNG", transparency=1234)
    out = np.asarray(_open_master(ingest(data, "key16.png", paths=paths)), dtype=np.int16)
    assert out[20, 10].min() >= 245  # transparent: white
    assert abs(int(out[20, 50, 0]) - round(40000 / 257)) <= 4  # scaled, not clipped


@pytest.mark.parametrize(
    ("mode", "info"),
    [
        ("P", {"transparency": 0}),
        ("P", {}),
        ("RGBA", {}),
        ("LA", {}),
        ("RGB", {"transparency": (0, 0, 0)}),
        ("I;16", {}),
        ("1", {}),
        ("RGB", {}),
    ],
)
@pytest.mark.parametrize("factor", [1, 3])
def test_normalise_in_strips_equals_the_whole_image(
    monkeypatch: pytest.MonkeyPatch, mode: str, info: dict, factor: int
) -> None:
    rng = np.random.default_rng(5)
    rgba = rng.integers(0, 256, (97, 131, 4), dtype=np.uint8)
    rgba[..., 3] = np.where(rng.random((97, 131)) < 0.3, 0, rgba[..., 3])
    src = Image.fromarray(rgba, "RGBA")
    if mode == "P":
        im = src.convert("RGB").quantize(16)
    elif mode == "I;16":
        im = Image.fromarray(np.asarray(src.convert("L"), dtype=np.uint16) * 257)
    else:
        im = src.convert(mode)
    im.info.update(info)
    monkeypatch.setattr(masters, "_STRIP_PIXELS", 7 * 131)  # many strips
    got = masters.normalise(im, factor)
    whole = masters._flatten_alpha(masters._to_8bit(im))
    want = whole.reduce(factor) if factor > 1 else whole
    assert got.mode == want.mode and got.mode in ("L", "RGB")
    assert np.array_equal(np.asarray(got), np.asarray(want))


def test_huge_jpeg_is_drafted(paths: DataPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    im = Image.new("RGB", (7400, 4000), (30, 90, 200))
    im.paste((250, 220, 40), (0, 0, 3700, 4000))
    seen: list[tuple[str, tuple[int, int]]] = []
    real = masters.decode_cost

    def spy(mode: str, size: tuple[int, int], fmt: str, *args: object, **kw: object) -> int:
        seen.append((mode, size))
        return real(mode, size, fmt, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(masters, "decode_cost", spy)
    m = ingest(jpeg_bytes(im, quality=80), "huge.jpg", paths=paths)
    assert seen == [("RGB", (1850, 1000))]  # libjpeg decoded at 1/4 scale
    assert (m.width, m.height) == (1800, 973)
    left, right = _open_master(m).getpixel((100, 500)), _open_master(m).getpixel((1700, 500))
    assert left[0] > 230 and right[2] > 180


def test_draft_size() -> None:
    assert masters._draft_size((2400, 1600), MASTER_LONG_EDGE) is None
    assert masters._draft_size((3500, 2000), MASTER_LONG_EDGE) is None  # 1/2 would undershoot
    assert masters._draft_size((8000, 6000), MASTER_LONG_EDGE) == (1800, 1350)
    assert masters._draft_size((6000, 8000), MASTER_LONG_EDGE) == (1350, 1800)


@pytest.mark.parametrize(
    ("size", "decoded"),
    [((5712, 4284), (2856, 2142)), ((8064, 6048), (2016, 1512)), ((4000, 3000), (2000, 1500))],
)
def test_phone_jpegs_decode_at_or_above_master_size(
    paths: DataPaths, size: tuple[int, int], decoded: tuple[int, int]
) -> None:
    im = Image.open(io.BytesIO(jpeg_bytes(Image.new("RGB", size, (90, 90, 90)), quality=50)))
    req = masters._draft_size(im.size, MASTER_LONG_EDGE)
    assert req is not None
    im.draft("RGB", req)
    assert im.size == decoded and max(decoded) >= MASTER_LONG_EDGE


def test_webp(paths: DataPaths) -> None:
    m = ingest(encode(gradient_image(1200, 800), "WEBP", quality=90), "a.webp", paths=paths)
    assert (m.width, m.height) == (1200, 800) and m.source_format == "webp"


def test_heic(paths: DataPaths) -> None:
    exif = Image.Exif()
    exif[0x0112] = 6
    src = gradient_image(1200, 800)
    data = heic_bytes(src, quality=90, exif=exif.tobytes())
    assert data[4:12] == b"ftypheic"
    m = ingest(data, "IMG_0001.HEIC", paths=paths)
    # pillow-heif stores the orientation as a HEIF transform that libheif
    # applies on decode; the master is upright and turned exactly once
    assert (m.width, m.height) == (800, 1200) and m.portrait is True
    assert m.source_format == "heif" and m.original_name == "IMG_0001.HEIC"
    got = np.asarray(_open_master(m), dtype=np.int16)
    want = np.asarray(src.transpose(Image.Transpose.ROTATE_270), dtype=np.int16)
    assert np.abs(got - want).mean() < 6


def test_large_heic_is_box_reduced_in_strips(paths: DataPaths) -> None:
    src = gradient_image(3700, 2000)
    data = heic_bytes(src, quality=90)
    m = ingest(data, "big.heic", paths=paths)
    assert (m.width, m.height) == (1800, 973)
    # the same picture as Pillow's plugin decoding it whole, then Lanczos
    with Image.open(io.BytesIO(data)) as whole:
        want = whole.convert("RGB").resize((1800, 973), Image.Resampling.LANCZOS)
    got = np.asarray(_open_master(m), dtype=np.int16)
    assert np.abs(got - np.asarray(want, dtype=np.int16)).mean() < 3


def test_gif_with_transparency(paths: DataPaths) -> None:
    im = Image.new("P", (80, 60), 0)
    im.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
    im.paste(1, (40, 0, 80, 60))
    data = encode(im, "GIF", transparency=0)
    m = ingest(data, "a.gif", paths=paths)
    out = _open_master(m)
    assert min(out.getpixel((10, 30))) >= 250  # transparent -> white
    assert out.getpixel((70, 30))[0] > 200


def test_16_bit_grey_png_is_scaled_not_clipped(paths: DataPaths) -> None:
    a = np.zeros((40, 60), np.uint16)
    a[:, 20:40] = 32896  # ~ 50 %
    a[:, 40:] = 65535
    im = Image.fromarray(a)
    assert im.mode.startswith("I;16")
    m = ingest(encode(im, "PNG"), "deep.png", paths=paths)
    arr = np.asarray(_open_master(m), dtype=np.int16)
    assert arr[20, 5].max() < 8 and abs(int(arr[20, 30, 0]) - 128) < 8 and arr[20, 55].min() > 247


def test_cmyk_jpeg_and_bmp_and_tiff(paths: DataPaths) -> None:
    cmyk = Image.new("CMYK", (90, 60), (0, 255, 255, 0))  # red
    m = ingest(jpeg_bytes(cmyk), "cmyk.jpg", paths=paths)
    r, g, b = _open_master(m).getpixel((45, 30))
    assert r > 200 and g < 60 and b < 60
    for fmt in ("BMP", "TIFF"):
        m = ingest(encode(Image.new("RGB", (90, 60), (0, 0, 255)), fmt), f"a.{fmt}", paths=paths)
        assert _open_master(m).getpixel((45, 30))[2] > 230


def test_icc_profile_is_honoured_or_ignored(paths: DataPaths) -> None:
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    img = Image.new("RGB", (90, 60), (200, 40, 40))
    m = ingest(jpeg_bytes(img, quality=95, icc_profile=srgb), "srgb.jpg", paths=paths)
    out = _open_master(m)
    assert "icc_profile" not in out.info
    assert all(abs(a - b) <= 4 for a, b in zip(out.getpixel((45, 30)), (200, 40, 40), strict=True))
    m = ingest(jpeg_bytes(img, icc_profile=b"not a profile" * 20), "bad.jpg", paths=paths)
    assert all(
        abs(a - b) <= 4
        for a, b in zip(_open_master(m).getpixel((45, 30)), (200, 40, 40), strict=True)
    )


def test_ids_and_delete(paths: DataPaths) -> None:
    m = ingest(jpeg_bytes(Image.new("RGB", (60, 40))), "a.jpg", paths=paths, photo_id="ab" * 16)
    assert m.id == "ab" * 16 and m.path.name == f"{'ab' * 16}.jpg"
    assert masters.delete_master(paths, m.id) is True
    assert not m.path.exists()
    assert masters.delete_master(paths, m.id) is False
    fresh = ingest(jpeg_bytes(Image.new("RGB", (60, 40))), "b.jpg", paths=paths)
    assert len(fresh.id) == 32 and int(fresh.id, 16) >= 0
    assert [p.name for p in paths.masters.iterdir()] == [f"{fresh.id}.jpg"]  # no temp files left


@pytest.mark.parametrize(
    "bad", ["../evil", "..", "/etc/passwd", "AB" * 16, "ab" * 15, "ab" * 16 + "/x", "", None]
)
def test_ids_never_become_paths(paths: DataPaths, bad: object) -> None:
    data = jpeg_bytes(Image.new("RGB", (60, 40)))
    with pytest.raises(ValueError, match="photo id"):
        ingest(data, "a.jpg", paths=paths, photo_id=bad or "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="photo id"):
        masters.master_path(paths, bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="photo id"):
        masters.delete_master(paths, bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="photo id"):
        masters.master_relpath(bad)  # type: ignore[arg-type]
    assert list(paths.masters.iterdir()) == []


def test_masters_are_never_replaced(paths: DataPaths) -> None:
    # render keys leave the master out because a master never changes
    m = ingest(jpeg_bytes(Image.new("RGB", (60, 40), (1, 2, 3))), "a.jpg", paths=paths)
    before = m.path.read_bytes()
    with pytest.raises(FileExistsError):
        ingest(
            jpeg_bytes(Image.new("RGB", (60, 40), (9, 9, 9))), "b.jpg", paths=paths, photo_id=m.id
        )
    assert m.path.read_bytes() == before


# --- names --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("IMG_1234.HEIC", "IMG_1234.HEIC"),
        ("C:\\Users\\adam\\Pictures\\beach.jpg", "beach.jpg"),
        ("../../etc/passwd", "passwd"),
        ("bad\x00name\x1b[31m.jpg", "badname[31m.jpg"),
        ("  lots   of\tspace .png ", "lots of space .png"),
        ("\u202egnp.exe", "gnp.exe"),  # right-to-left override stripped
        ("", "photo"),
        (None, "photo"),
        ("\x00\x01", "photo"),
    ],
)
def test_sanitize_name(raw: object, clean: str) -> None:
    assert sanitize_name(raw) == clean


def test_sanitize_name_truncates_keeping_extension() -> None:
    name = "a" * 300 + ".jpeg"
    out = sanitize_name(name)
    assert len(out) == masters.MAX_NAME_CHARS and out.endswith(".jpeg")
    assert len(sanitize_name("b" * 300)) == masters.MAX_NAME_CHARS


# --- MPO: a JPEG whose MPF segment lists more images ------------------------------------


def _mpo(image: Image.Image, **kw: Any) -> bytes:
    """``image`` as an MPO: a JPEG with an MPF segment and a second image
    after it, which is how phones store previews and gain maps."""
    return encode(image, "MPO", save_all=True, append_images=[image.resize((64, 48))], **kw)


def test_progressive_mpo_counts_its_coefficient_buffer() -> None:
    # Pillow's JPEG factory opens a JPEG whose MPF segment lists two or more
    # images as an MpoImageFile (format "MPO"); its primary image goes
    # through the same libjpeg path, coefficient buffer included
    im = gradient_image(640, 480)
    plain = encode(im, "JPEG", quality=85, progressive=True, subsampling=0)
    mpo = _mpo(im, quality=85, progressive=True, subsampling=0)
    with Image.open(io.BytesIO(plain), formats=["JPEG"]) as opened:
        expected = masters.decoder_overhead(opened, plain)
    assert expected == 640 * 480 * 6  # 4:4:4
    with Image.open(io.BytesIO(mpo), formats=["JPEG"]) as opened:
        assert opened.format == "MPO"
        assert masters.decoder_overhead(opened, mpo) == expected
    baseline = _mpo(im, quality=85)
    with Image.open(io.BytesIO(baseline), formats=["JPEG"]) as opened:
        assert opened.format == "MPO" and masters.decoder_overhead(opened, baseline) == 0


def test_large_mpo_is_drafted(paths: DataPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    im = Image.new("RGB", (7400, 4000), (30, 90, 200))
    im.paste((250, 220, 40), (0, 0, 3700, 4000))
    seen: list[tuple[str, tuple[int, int], object]] = []
    real = masters.decode_cost

    def spy(mode: str, size: tuple[int, int], fmt: str, *args: Any, **kw: Any) -> int:
        seen.append((mode, size, kw.get("decoder_bytes")))
        return real(mode, size, fmt, *args, **kw)

    monkeypatch.setattr(masters, "decode_cost", spy)
    m = ingest(_mpo(im, quality=80), "phone.jpg", paths=paths)
    assert seen == [("RGB", (1850, 1000), 0)]  # libjpeg at 1/4 scale, no coefficient buffer
    assert m.source_format == "jpeg" and (m.width, m.height) == (1800, 973)
    assert _open_master(m).getpixel((100, 500))[0] > 230


# --- decoders that run in Python --------------------------------------------------------


def _rle8_bmp(width: int, height: int) -> bytes:
    """An 8-bit RLE BMP whose rows are an 8-pixel run of grey 200 and an
    end-of-line: a tiny file that makes Pillow's Python decoder pad
    ``width - 8`` bytes a row, one loop step at a time."""
    palette = b"".join(bytes((i, i, i, 0)) for i in range(256))
    pixels = b"\x08\xc8\x00\x00" * height + b"\x00\x01"
    offset = 14 + 40 + len(palette)
    file_header = struct.pack("<2sIHHI", b"BM", offset + len(pixels), 0, 0, offset)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 8, 1, len(pixels), 2835, 2835, 256, 0)
    return file_header + dib + palette + pixels


def test_rle_bmp_is_limited(paths: DataPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    decoded: list[int] = []
    real = BmpImagePlugin.BmpRleDecoder.decode

    def spy(self: BmpImagePlugin.BmpRleDecoder, buffer: Any) -> tuple[int, int]:
        decoded.append(1)
        return real(self, buffer)

    monkeypatch.setattr(BmpImagePlugin.BmpRleDecoder, "decode", spy)
    m = ingest(_rle8_bmp(300, 200), "small.bmp", paths=paths)
    out = np.asarray(_open_master(m).convert("L"), dtype=np.int16)
    assert (m.width, m.height) == (300, 200) and out[100, 2] > 170 and out[100, 150] < 30
    assert decoded == [1]
    side = int(masters.MAX_RLE_BMP_PIXELS**0.5) + 1  # 2001 x 2001: a 9 KB file
    t0 = time.perf_counter()
    with pytest.raises(UndecodableImage, match="RLE") as exc:
        ingest(_rle8_bmp(side, side), "big.bmp", paths=paths)
    assert exc.value.status == 422 and time.perf_counter() - t0 < 1
    assert decoded == [1]  # refused before the decoder ran


def test_float_tiff_is_scaled_from_0_to_1(paths: DataPaths) -> None:
    ramp = np.tile(np.linspace(0, 1, 90, dtype=np.float32), (60, 1))
    for samples in (ramp, ramp * 255):  # 0..1 is scaled; anything else is taken as 0..255
        im = Image.fromarray(samples)
        assert im.mode == "F"
        m = ingest(encode(im, "TIFF"), "float.tif", paths=paths)
        grey = np.asarray(_open_master(m).convert("L"), dtype=np.int16)
        assert grey[30, 0] < 10 and grey[30, -1] > 245 and abs(int(grey[30, 45]) - 128) < 10


def test_compressed_tiff_decodes_without_copying_the_file() -> None:
    # libtiff's in-memory path decodes from fp.getvalue(); a BytesIO made from
    # bytes shares them, so the budget is right to count the upload once
    noise = np.random.default_rng(2).integers(0, 256, (600, 800, 3), dtype=np.uint8)
    data = encode(Image.fromarray(noise), "TIFF", compression="tiff_adobe_deflate")
    assert len(data) > 1_000_000
    with Image.open(io.BytesIO(data), formats=["TIFF"]) as im:
        tracemalloc.start()
        try:
            im.load()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    assert peak < len(data) // 4


# --- header structure ------------------------------------------------------------------------


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _png_with(before: Sequence[bytes] = (), after: Sequence[bytes] = ()) -> bytes:
    """A 64 x 48 RGB PNG with extra chunks before and after its pixel data."""
    png = encode(Image.new("RGB", (64, 48), (9, 90, 200)), "PNG")
    pixels = 8 + 25  # after the signature and IHDR
    iend = png.rindex(b"IEND") - 4
    return png[:pixels] + b"".join(before) + png[pixels:iend] + b"".join(after) + png[iend:]


def _gif_with(blocks: bytes) -> bytes:
    """A 16 x 16 GIF with ``blocks`` (extensions) before its image."""
    gif = encode(Image.new("P", (16, 16)), "GIF")
    flags = gif[10]
    head = 13 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0)
    return gif[:head] + blocks + gif[head:]


def _gif_comment(size: int) -> bytes:
    full, rest = divmod(size, 255)
    body = (b"\xff" + b"c" * 255) * full + (bytes([rest]) + b"c" * rest if rest else b"")
    return b"\x21\xfe" + body + b"\x00"


def test_png_text_is_counted(paths: DataPaths) -> None:
    # Pillow keeps every text chunk's value in info/text until the master is
    # encoded, and a zTXt chunk of 1 KB inflates to its 1 MB limit
    bomb = zlib.compress(b"B" * 1_000_000, 9)
    notes = [_png_chunk(b"zTXt", b"note%02d\x00\x00" % i + bomb) for i in range(20)]
    data = _png_with(before=notes[:10], after=notes[10:])
    meta = masters.scan_metadata(data, "png")
    assert meta.retained >= 20 * 1_000_000 and len(data) < 50_000
    with pytest.raises(UndecodableImage, match="metadata"):
        ingest(data, "notes.png", paths=paths, max_decode_bytes=20_000_000)
    m = ingest(data, "notes.png", paths=paths)  # the default budget has room for it
    assert (m.width, m.height) == (64, 48)
    # counted for every stage, not only the decode
    cost = masters.decode_cost
    assert cost("RGB", (64, 48), "png", retained_bytes=meta.retained) > meta.retained
    assert masters.scan_metadata(encode(Image.new("RGB", (8, 8)), "PNG"), "png") == (
        masters.Metadata()
    )


def test_png_metadata_scan() -> None:
    def scan(*chunks: bytes) -> masters.Metadata:
        return masters.scan_metadata(_png_with(before=list(chunks)), "png")

    text = scan(_png_chunk(b"tEXt", b"Comment\x00" + b"x" * 5000))
    assert 5000 <= text.retained <= 4 * 5000 and text.transient >= 2 * 5000
    # UTF-8 iTXt becomes a str of up to 4 bytes a character
    plain = scan(_png_chunk(b"iTXt", b"Desc\x00\x00\x00\x00\x00" + b"x" * 5000))
    wide = scan(
        _png_chunk(b"iTXt", b"Desc\x00\x00\x00\x00\x00" + b"x" * 5000 + "\U0001f600".encode())
    )
    assert wide.retained > 3 * plain.retained
    zipped = scan(_png_chunk(b"iTXt", b"Desc\x00\x01\x00\x00\x00" + zlib.compress(b"x" * 5000)))
    assert zipped.retained >= 5000
    # private chunks are kept whole; APNG's own are not
    assert scan(_png_chunk(b"prVt", b"p" * 5000)).retained >= 5000
    assert scan(_png_chunk(b"fcTL", b"\x00" * 26)).retained == 0


def test_header_floods_are_refused_before_pillow_parses_them(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pillow parses these headers in Python, one item at a time, inside
    # Image.open: a 25 MB JPEG of empty APP1 segments would take seconds and
    # keep ~750 MB of objects before any check after the open could run
    opened: list[object] = []
    real_open = Image.open

    def spy(*args: Any, **kw: Any) -> Image.Image:
        opened.append(kw.get("formats"))
        return real_open(*args, **kw)

    monkeypatch.setattr(Image, "open", spy)
    monkeypatch.setattr(masters, "MAX_HEADER_STEPS", 500)
    jpeg = jpeg_bytes(Image.new("RGB", (16, 16), (200, 10, 10)))
    first = 4 + int.from_bytes(jpeg[4:6], "big")  # after the APP0 segment
    floods = {
        "APP1": jpeg[:2] + b"\xff\xe1\x00\x02" * 501 + jpeg[2:],
        "junk": jpeg[:first] + b"\x00" * 501 + jpeg[first:],
        "chunks": _png_with(before=[_png_chunk(b"tEXt", b"k%04d\x00" % i) for i in range(501)]),
        "gif": _gif_with(b"\x21\xfe\x00" * 501),
    }
    for label, data in floods.items():
        with pytest.raises(UndecodableImage, match="more than 500") as exc:
            ingest(data, label, paths=paths)
        assert exc.value.status == 422, label
    assert opened == []
    # a few hundred are fine
    ingest(jpeg[:2] + b"\xff\xe1\x00\x02" * 400 + jpeg[2:], "ok.jpg", paths=paths)
    ingest(jpeg[:first] + b"\x00" * 400 + jpeg[first:], "ok.jpg", paths=paths)
    assert len(opened) == 2


def test_gif_comments_are_limited(paths: DataPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pillow joins a comment one 255-byte block at a time: quadratic in its
    # length (a 4 MB comment takes 5 s on the desktop, 25 MB would take minutes)
    monkeypatch.setattr(masters, "MAX_GIF_COMMENT_COPY", 1_000_000)
    with pytest.raises(UndecodableImage, match="comments"):
        ingest(_gif_with(_gif_comment(50_000)), "long.gif", paths=paths)  # ~5 MB copied
    data = _gif_with(_gif_comment(5_000) + _gif_comment(300))
    assert masters.scan_metadata(data, "gif").retained == 5_301  # joined with a newline
    m = ingest(data, "short.gif", paths=paths)
    assert (m.width, m.height) == (16, 16)


def test_jpeg_metadata_is_counted() -> None:
    exif = Image.Exif()
    exif[0x010E] = "x" * 20_000  # ImageDescription
    data = jpeg_bytes(Image.new("RGB", (16, 16)), exif=exif.tobytes())
    assert masters.scan_metadata(data, "jpeg").retained >= 2 * 20_000
    assert masters.scan_metadata(jpeg_bytes(Image.new("RGB", (16, 16))), "jpeg").retained < 1_000


# --- EXIF/TIFF IFD walker ----------------------------------------------------------------


def _ifd(
    entries: Sequence[tuple[int, int, int, int]], *, first: int = 8, next_off: int = 0
) -> bytes:
    """A little-endian classic-TIFF block: a header pointing at one IFD of
    ``(tag, type, count, value)`` entries.  The walker only sums
    ``count x type_size`` and never dereferences a value, so the regions the
    offsets point at need not exist; a little trailing padding keeps any small
    ``first``/``next`` offset in range."""
    body = (
        struct.pack("<H", len(entries))
        + b"".join(struct.pack("<HHII", t, typ, c, v) for t, typ, c, v in entries)
        + struct.pack("<I", next_off)
    )
    blob = b"II*\x00" + struct.pack("<I", first) + body
    return blob + b"\x00" * 64


def test_ifd_walker_sums_materialised_bytes() -> None:
    walk = masters._ifd_materialised_bytes
    # count x type_size per entry: SHORT(2)x2 + LONG(4)x1 + UNDEFINED(1)x10 + RATIONAL(8)x3
    got = walk(_ifd([(1, 3, 2, 0), (2, 4, 1, 0), (3, 7, 10, 0), (4, 5, 3, 0)]))
    assert got == 2 * 2 + 1 * 4 + 10 * 1 + 3 * 8
    # an unknown field type counts as one byte a value, never zero
    assert walk(_ifd([(9, 999, 50, 0)])) == 50
    # a big-endian block is read the same way
    be = b"MM\x00\x2a\x00\x00\x00\x08" + struct.pack(">H", 1) + struct.pack(">HHII", 1, 4, 7, 0)
    be += struct.pack(">I", 0) + b"\x00" * 64
    assert walk(be) == 7 * 4


def test_ifd_walker_follows_sub_ifds_and_the_next_chain() -> None:
    walk = masters._ifd_materialised_bytes
    # IFD0 has an Exif pointer (0x8769) to a sub-IFD, and a next IFD (IFD1)
    sub_off, ifd1_off = 100, 200
    ifd0 = b"II*\x00" + struct.pack("<I", 8)
    ifd0 += struct.pack("<H", 2)
    ifd0 += struct.pack("<HHII", 0x8769, 4, 1, sub_off)  # Exif IFD pointer: 4 bytes (count 1, LONG)
    ifd0 += struct.pack("<HHII", 0x010E, 7, 5, 0)  # 5 bytes in IFD0
    ifd0 += struct.pack("<I", ifd1_off)  # next: IFD1
    data = bytearray(ifd0 + b"\x00" * 300_000)
    data[sub_off : sub_off + 2] = struct.pack("<H", 1)
    data[sub_off + 2 : sub_off + 14] = struct.pack(
        "<HHII", 0x9286, 7, 70, 0
    )  # 70 bytes in the Exif IFD
    data[sub_off + 14 : sub_off + 18] = struct.pack("<I", 0)
    data[ifd1_off : ifd1_off + 2] = struct.pack("<H", 1)
    data[ifd1_off + 2 : ifd1_off + 14] = struct.pack(
        "<HHII", 0x0201, 7, 900, 0
    )  # 900 bytes in IFD1
    data[ifd1_off + 14 : ifd1_off + 18] = struct.pack("<I", 0)
    # every entry's value is materialised, the pointer entry's own 4 bytes included
    assert walk(bytes(data)) == (4 + 5) + 70 + 900


def test_ifd_walker_refuses_too_many_entries() -> None:
    bomb = _ifd([(40000 + i, 7, 1_000, 1) for i in range(2001)])
    with pytest.raises(UndecodableImage, match="2000"):
        masters._ifd_materialised_bytes(bomb)
    # 2000 is allowed (it is the cap, not past it)
    assert (
        masters._ifd_materialised_bytes(_ifd([(40000 + i, 7, 1, 1) for i in range(2000)])) == 2000
    )


def test_ifd_walker_refuses_a_long_ifd_chain() -> None:
    # 40 empty IFDs chained through the next pointer: past the 32-IFD cap
    buf = bytearray(b"II*\x00" + struct.pack("<I", 8))
    offs = [8 + 6 * i for i in range(40)]
    for i, off in enumerate(offs):
        nxt = offs[i + 1] if i + 1 < len(offs) else 0
        while len(buf) < off:
            buf += b"\x00"
        buf += struct.pack("<H", 0) + struct.pack("<I", nxt)
    with pytest.raises(UndecodableImage, match="IFDs"):
        masters._ifd_materialised_bytes(bytes(buf))


def test_ifd_walker_stops_on_malformed_offsets() -> None:
    walk = masters._ifd_materialised_bytes
    assert walk(b"II*\x00" + struct.pack("<I", 99_999) + b"\x00" * 32) == 0  # first IFD past EOF
    assert walk(b"XY*\x00" + b"\x00" * 32) == 0  # not a byte order mark
    assert walk(b"II") == 0  # too short
    # a cycle (IFD0.next -> IFD0) is visited once, not forever
    cyc = _ifd([(1, 7, 3, 0)], next_off=8)
    assert walk(cyc) == 3


def test_block_locators_find_each_format_exif() -> None:
    ifd = _ifd([(0x0112, 3, 1, 6)])  # a one-tag IFD
    jpeg = io.BytesIO()
    Image.new("RGB", (8, 8)).save(jpeg, "JPEG")
    j = jpeg.getvalue()
    payload = b"Exif\x00\x00" + ifd
    with_app1 = j[:2] + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + j[2:]
    assert masters._jpeg_exif_block(with_app1).startswith(b"Exif\x00\x00II*\x00")
    assert masters._jpeg_exif_block(j) == b""  # no EXIF

    png = io.BytesIO()
    Image.new("RGB", (8, 8)).save(png, "PNG")
    p = png.getvalue()
    chunk = (
        struct.pack(">I", len(ifd)) + b"eXIf" + ifd + struct.pack(">I", zlib.crc32(b"eXIf" + ifd))
    )
    assert masters._png_exif_blocks(p[:33] + chunk + p[33:]) == [ifd]
    assert masters._png_exif_blocks(p) == []
    # Pillow keeps the LAST eXIf chunk (even one after IDAT): every chunk is charged
    assert masters._png_exif_blocks(p[:33] + chunk + p[33:-12] + chunk + p[-12:]) == [ifd, ifd]

    webp = io.BytesIO()
    Image.new("RGB", (8, 8)).save(webp, "WEBP", exif=ifd)
    assert masters._png_exif_blocks(b"") == []
    assert masters._webp_exif_block(webp.getvalue()).endswith(ifd[8:])  # header then our one tag


# --- real EXIF still ingests and orients (positive) --------------------------------------


def test_real_exif_jpeg_still_ingests_and_orients(paths: DataPaths) -> None:
    # a portrait JPEG with a genuine Pillow EXIF block: orientation 6 turns it
    # landscape, a handful of other tags come along, and it ingests normally
    a = np.full((200, 120, 3), 160, np.uint8)
    a[:40, :30] = (255, 0, 0)  # red in the stored top-left corner
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90 CW to display
    exif[0x010F] = "Inkframe"  # Make
    exif[0x0110] = "Test Camera"  # Model
    exif[0x0131] = "pytest"  # Software
    data = jpeg_bytes(Image.fromarray(a), quality=95, exif=exif.tobytes())
    assert masters.scan_metadata(data, "jpeg").retained < 5_000  # a real IFD is tiny
    m = ingest(data, "real.jpg", paths=paths)
    assert (m.width, m.height) == (200, 120) and m.portrait is False
    im = _open_master(m)
    assert "exif" not in im.info  # applied and dropped
    assert im.getpixel((200 - 15, 15))[0] > 200  # red moved to the top right


def test_real_exif_png_still_ingests_and_orients(paths: DataPaths) -> None:
    a = np.full((200, 120, 3), 160, np.uint8)
    a[:40, :30] = (255, 0, 0)
    exif = Image.Exif()
    exif[0x0112] = 8  # rotate 90 CCW
    data = encode(Image.fromarray(a), "PNG", exif=exif.tobytes())
    assert masters._png_exif_blocks(data)  # the eXIf chunk is really there
    m = ingest(data, "real.png", paths=paths)
    assert (m.width, m.height) == (200, 120) and m.portrait is False
    expected = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    assert np.abs(np.asarray(_open_master(m), np.int16) - np.asarray(expected, np.int16)).mean() < 2


def test_jpeg_with_a_large_legitimate_icc_segment_still_ingests(paths: DataPaths) -> None:
    # a 2 MB ICC profile is spread across APP2 markers (not an EXIF IFD): it is
    # not charged against the IFD walker and ingests under the default budget
    icc = b"\x00" * 2_000_000
    data = jpeg_bytes(Image.new("RGB", (64, 64), (10, 120, 200)), icc_profile=icc)
    assert len(data) > 2_000_000
    assert masters._jpeg_exif_block(data) == b""  # ICC is APP2, not APP1 Exif
    m = ingest(data, "wide-gamut.jpg", paths=paths)
    assert (m.width, m.height) == (64, 64)


def test_exif_behind_a_junk_byte_is_still_charged(paths: DataPaths) -> None:
    # Pillow steps over junk between markers and goes on parsing, so an EXIF
    # block behind one junk byte still reaches getexif(); the EXIF locator
    # used to stop at the junk (a 1000-tag IFD bomb was charged 100 KB, and
    # Pillow materialised 50 MB of it)
    jpeg = jpeg_bytes(Image.new("RGB", (16, 16), (200, 10, 10)))
    first = 4 + int.from_bytes(jpeg[4:6], "big")  # after the APP0 segment
    payload = b"Exif\x00\x00" + _ifd([(40000 + i, 7, 50_000, 8) for i in range(1000)])
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    data = jpeg[:first] + b"\x00" + app1 + jpeg[first:]
    assert masters._jpeg_exif_block(data) == payload
    assert masters.scan_metadata(data, "jpeg").retained >= 1000 * 50_000
    with pytest.raises(UndecodableImage, match="metadata"):
        ingest(data, "junk.jpg", paths=paths, max_decode_bytes=20_000_000)


def _spy_open(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record every ``Image.open`` (its ``formats``) from now on."""
    opened: list[object] = []
    real_open = Image.open

    def spy(*args: Any, **kw: Any) -> Image.Image:
        opened.append(kw.get("formats"))
        return real_open(*args, **kw)

    monkeypatch.setattr(Image, "open", spy)
    return opened


# --- TIFF strips ------------------------------------------------------------------------


def _strip_tiff(rows: int) -> bytes:
    """An 8 x ``rows`` grey uncompressed TIFF stored one row a strip that
    stays small however many rows it has (``rows`` > 2): every strip is the
    same 8-byte row at offset 8, and one array of SHORT 8s is both its
    StripOffsets and its StripByteCounts.  Pillow opens and decodes it,
    building ~320 bytes of Python objects per strip."""
    entries = [
        (256, 3, 1, 8),  # ImageWidth
        (257, 4, 1, rows),  # ImageLength
        (258, 3, 1, 8),  # BitsPerSample
        (259, 3, 1, 1),  # Compression: none
        (262, 3, 1, 1),  # PhotometricInterpretation: BlackIsZero
        (273, 3, rows, 0),  # StripOffsets -> the array
        (277, 3, 1, 1),  # SamplesPerPixel
        (278, 4, 1, 1),  # RowsPerStrip
        (279, 3, rows, 0),  # StripByteCounts -> the same array
    ]
    array = 16 + 2 + 12 * len(entries) + 4
    ifd = struct.pack("<H", len(entries)) + b"".join(
        struct.pack("<HHII", tag, typ, count, value or array) for tag, typ, count, value in entries
    )
    head = b"II*\x00" + struct.pack("<I", 16) + b"\x80" * 8
    return head + ifd + b"\x00" * 4 + struct.pack("<H", 8) * rows


def test_tiff_strip_flood_is_refused_before_pillow_parses_it(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the builder makes a real TIFF: Pillow decodes it, one tile per strip
    with Image.open(io.BytesIO(_strip_tiff(1000)), formats=["TIFF"]) as im:
        assert len(im.tile) == 1000
        im.load()
        assert im.size == (8, 1000) and im.getpixel((3, 999)) == 0x80
    assert ingest(_strip_tiff(1000), "rows.tif", paths=paths).height == 1000
    # Pillow builds a tile, its extents and two ints per strip at Image.open
    # (~320 bytes): 3 million one-row strips (24 MB of them) peaked at 914 MB
    opened = _spy_open(monkeypatch)
    data = _strip_tiff(3_000_000)
    assert len(data) < 7_000_000
    t0 = time.perf_counter()
    with pytest.raises(UndecodableImage, match="3000000 strips") as exc:
        ingest(data, "strips.tif", paths=paths)
    assert exc.value.status == 422 and time.perf_counter() - t0 < 1
    assert opened == []
    # up to the limit they are charged; past it the file is refused
    limit = masters.MAX_TIFF_STRIPS
    assert masters.scan_metadata(_strip_tiff(limit), "tiff").retained >= limit * 320
    with pytest.raises(UndecodableImage, match="strips or tiles"):
        masters.scan_metadata(_strip_tiff(limit + 1), "tiff")


def test_strip_and_tile_counts_come_from_every_strip_tag() -> None:
    for tag in (0x0111, 0x0117, 0x0144, 0x0145):  # Strip/Tile Offsets/ByteCounts
        assert masters._walk_ifds(_ifd([(tag, 4, 5000, 0)]))[1] == 5000
        with pytest.raises(UndecodableImage, match="strips or tiles"):
            masters.scan_metadata(_ifd([(tag, 4, masters.MAX_TIFF_STRIPS + 1, 0)]), "tiff")
    assert masters._walk_ifds(_ifd([(0x0112, 3, 1, 1)]))[1] == 0  # Orientation is not one
    # the largest count of any of them, not their sum
    assert masters._walk_ifds(_ifd([(0x0111, 4, 300, 0), (0x0117, 3, 200, 0)]))[1] == 300
    # an EXIF block is not charged per strip (getexif builds no tiles)
    exif = _png_chunk(b"eXIf", _ifd([(0x0111, 4, 9, 0)]))
    assert masters.scan_metadata(_png_with(before=[exif]), "png").retained < 9 * 320


@pytest.mark.parametrize("rows_per_strip", [16, None])
def test_striped_and_single_strip_tiffs_still_ingest(
    paths: DataPaths, rows_per_strip: int | None
) -> None:
    kw: dict[str, Any] = {"tiffinfo": {278: rows_per_strip}} if rows_per_strip else {}
    data = encode(gradient_image(2400, 1600), "TIFF", **kw)
    strips = 1600 // rows_per_strip if rows_per_strip else 1
    assert masters._walk_ifds(data)[1] == strips
    meta = masters.scan_metadata(data, "tiff")
    assert strips * 320 <= meta.retained < strips * 320 + 10_000
    m = ingest(data, "scan.tif", paths=paths)
    assert (m.width, m.height) == (1800, 1200)


def _tiff_load_peak(data: bytes) -> int:
    """tracemalloc's peak while Pillow opens and loads the TIFF ``data`` and
    its orientation is read, as ``_decode_pillow`` does."""
    tracemalloc.start()
    try:
        with Image.open(io.BytesIO(data), formats=["TIFF"]) as im:
            im.load()
            masters._orientation(im)
            return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_tiff_tag_data_is_charged_twice() -> None:
    # Pillow reads a TIFF's IFD0 into tag_v2 at Image.open and again for
    # getexif() while it loads the pixels: both copies live until the master
    im = Image.new("RGB", (64, 64), (1, 2, 3))
    data = encode(im, "TIFF", tiffinfo={40000 + i: b"x" * 50_000 for i in range(20)})
    charged = masters.scan_metadata(data, "tiff").retained
    assert charged >= 2 * 20 * 50_000
    built = _tiff_load_peak(data) - _tiff_load_peak(encode(im, "TIFF"))
    # the entries' own dict slots (~350 bytes each) are left to the margin
    assert 2 * 20 * 50_000 <= built <= charged * masters._COST_MARGIN


# --- the MPF segment's MP index -----------------------------------------------------------


def _mpf_index(n_tags: int, typ: int, count: int, size: int = 65_000) -> bytes:
    """A little-endian MP index (the TIFF block of an APP2 ``MPF\\0``
    segment) of ``n_tags`` tags of field type ``typ``, each holding
    ``count`` values read from offset 8 (the index itself, padded to
    ``size`` bytes with noise, so the values are not cached small ints)."""
    entries = b"".join(struct.pack("<HHII", 40000 + i, typ, count, 8) for i in range(n_tags))
    blob = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", n_tags) + entries + b"\x00" * 4
    noise = np.random.default_rng(1).integers(1, 256, max(0, size - len(blob)), np.uint8)
    return blob + noise.tobytes()


def _jpeg_with_mpf(index: bytes) -> bytes:
    """A 16 x 16 JPEG with ``index`` in an APP2 ``MPF\\0`` segment."""
    jpeg = jpeg_bytes(Image.new("RGB", (16, 16), (200, 10, 10)))
    payload = b"MPF\x00" + index
    assert len(payload) + 2 <= 0xFFFF
    return jpeg[:2] + b"\xff\xe2" + struct.pack(">H", len(payload) + 2) + payload + jpeg[2:]


def test_mp_index_bombs_are_refused_before_pillow_parses_them(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pillow parses every JPEG's MP index inside Image.open (``_getmp``):
    # 5450 tags all reading one 65 KB region made a 66 KB JPEG peak at 356 MB
    opened = _spy_open(monkeypatch)
    flood = _jpeg_with_mpf(_mpf_index(5450, 7, 65_000))
    assert len(flood) < 70_000
    with pytest.raises(UndecodableImage, match="5450 tags") as exc:
        ingest(flood, "mpf.jpg", paths=paths)
    assert exc.value.status == 422
    # and it unpacks every value into a Python object (``dict(ifd)``), where
    # getexif keeps raw bytes: 1800 tags of 16 000 LONGs are 115 MB of raw
    # values, which the default budget would let in, but 1.3 GB of ints
    longs = _jpeg_with_mpf(_mpf_index(1800, 4, 16_000))
    assert len(longs) < 70_000
    assert masters.scan_metadata(longs, "jpeg").retained >= 1800 * 16_000 * 44
    with pytest.raises(UndecodableImage, match="metadata"):
        ingest(longs, "mpf.jpg", paths=paths)
    assert opened == []


def _open_peak(data: bytes) -> int:
    """tracemalloc's peak while ``Image.open`` parses the JPEG ``data``."""
    tracemalloc.start()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # "malformed MPO": no image count in the index
            with Image.open(io.BytesIO(data), formats=["JPEG"]):
                return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("typ", [1, 2, 3, 4, 5, 6, 7, 11, 12])  # BYTE, ASCII ... DOUBLE
def test_mp_index_charge_covers_what_pillow_builds(typ: int) -> None:
    size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 11: 4, 12: 8}[typ]
    data = _jpeg_with_mpf(_mpf_index(20, typ, 8000 // size, size=9000))
    charged = masters.scan_metadata(data, "jpeg").retained
    built = _open_peak(data) - _open_peak(jpeg_bytes(Image.new("RGB", (16, 16), (200, 10, 10))))
    assert 20 * 8000 <= built <= charged


def test_ifd_walker_counts_unpacked_values_for_an_mp_index() -> None:
    walk = masters._ifd_materialised_bytes
    assert walk(_ifd([(1, 4, 1000, 0)])) == 4000  # getexif: raw bytes only
    # measured: 38 to 44 bytes a SHORT/LONG/FLOAT/DOUBLE, ~245 a RATIONAL,
    # ~440 an entry and ~680 per image of the MP Entry list
    assert walk(_ifd([(1, 4, 1000, 0)]), objects=True) >= 440 + 1000 * 44
    assert walk(_ifd([(1, 3, 1000, 0)]), objects=True) >= 440 + 1000 * 38
    assert walk(_ifd([(1, 5, 1000, 0)]), objects=True) >= 440 + 1000 * 245
    assert walk(_ifd([(1, 2, 1000, 0)]), objects=True) >= 440 + 1000 * 2.4  # copied as a str
    assert walk(_ifd([(1, 7, 1000, 0)]), objects=True) < 1000 + 1000  # UNDEFINED stays bytes
    assert walk(_ifd([(0xB002, 7, 16 * 10, 0)]), objects=True) >= 10 * 680


def test_benign_mpo_still_ingests(paths: DataPaths) -> None:
    # what phones write: a primary JPEG whose MP index lists it and a preview
    im = gradient_image(640, 480)
    for data in (_mpo(im, quality=85), _mpo(im, quality=85, progressive=True)):
        head = masters._walk_jpeg(data)
        assert len(head.mpf) == 1 and head.mpf[0][:4] in (b"II*\x00", b"MM\x00*")
        assert masters.scan_metadata(data, "jpeg").retained < 20_000
        m = ingest(data, "phone.jpg", paths=paths)
        assert (m.width, m.height) == (640, 480) and m.source_format == "jpeg"


# --- JPEG scans and tables ----------------------------------------------------------------


def _with_scans(jpeg: bytes, extra: int) -> bytes:
    """``jpeg`` with its last scan (the SOS header and its entropy-coded
    data) repeated ``extra`` more times before EOI.  libjpeg still decodes
    it, one more pass over the image per scan (a 5 KB file of 210 scans
    decodes 28 times slower than its 10-scan original)."""
    last = jpeg.rindex(b"\xff\xda")
    assert jpeg.endswith(b"\xff\xd9")
    return jpeg[:-2] + jpeg[last:-2] * extra + jpeg[-2:]


def test_jpeg_scans_are_limited(paths: DataPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    im = gradient_image(320, 240)
    baseline = jpeg_bytes(im)  # one scan
    progressive = jpeg_bytes(im, progressive=True)  # libjpeg's script: 10 scans
    opened = _spy_open(monkeypatch)
    for data in (baseline, progressive):
        assert ingest(data, "ok.jpg", paths=paths).width == 320
    flood = _with_scans(progressive, 200)
    t0 = time.perf_counter()
    with pytest.raises(UndecodableImage, match="more than 64 scans") as exc:
        ingest(flood, "scans.jpg", paths=paths)
    assert exc.value.status == 422 and time.perf_counter() - t0 < 1
    assert len(opened) == 2  # refused before Pillow opened it
    # the limit is inclusive, and scans are counted up to the first EOI: an
    # MPO's later images (each with its own scans) are not decoded
    monkeypatch.setattr(masters, "MAX_JPEG_SCANS", 10)
    ingest(progressive, "ten.jpg", paths=paths)
    ingest(_mpo(im, progressive=True), "two-images.jpg", paths=paths)
    with pytest.raises(UndecodableImage, match="more than 10 scans"):
        ingest(_with_scans(progressive, 1), "eleven.jpg", paths=paths)
    # restart markers and stuffed bytes inside a scan are not scans
    restarts = encode(gradient_image(640, 480), "JPEG", quality=95, restart_marker_rows=1)
    assert sum(restarts.count(bytes((0xFF, 0xD0 + n))) for n in range(8)) > 20
    ingest(restarts, "restarts.jpg", paths=paths)


def _dqt(tables: int) -> bytes:
    """A DQT segment of ``tables`` 8-bit quantisation tables (all id 0)."""
    payload = (b"\x00" + bytes(range(1, 65))) * tables
    return b"\xff\xdb" + struct.pack(">H", len(payload) + 2) + payload


def test_jpeg_tables_are_limited(paths: DataPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pillow parses every DQT table before the first scan in Python (25 MB of
    # them take 0.8 s on the desktop); a segment can hold a thousand
    jpeg = jpeg_bytes(Image.new("RGB", (16, 16), (200, 10, 10)))  # 2 DQT and 4 DHT tables
    first = 4 + int.from_bytes(jpeg[4:6], "big")  # after the APP0 segment
    limit = masters.MAX_JPEG_TABLES
    opened = _spy_open(monkeypatch)
    for extra in (_dqt(1) * (limit - 5), _dqt(limit - 5)):  # one table over, either way
        with pytest.raises(UndecodableImage, match="tables") as exc:
            ingest(jpeg[:first] + extra + jpeg[first:], "tables.jpg", paths=paths)
        assert exc.value.status == 422
    assert opened == []
    for extra in (_dqt(1) * (limit - 6), _dqt(limit - 6)):  # exactly at the limit
        ingest(jpeg[:first] + extra + jpeg[first:], "tables.jpg", paths=paths)
    # the Huffman tables between a progressive JPEG's scans count too
    progressive = jpeg_bytes(gradient_image(320, 240), progressive=True)  # 4 + 8 tables
    monkeypatch.setattr(masters, "MAX_JPEG_TABLES", 12)
    ingest(progressive, "twelve.jpg", paths=paths)
    monkeypatch.setattr(masters, "MAX_JPEG_TABLES", 11)
    with pytest.raises(UndecodableImage, match="more than 11"):
        ingest(progressive, "twelve.jpg", paths=paths)
