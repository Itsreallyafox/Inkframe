"""Upload ingest: bytes from the browser -> a stored master JPEG.

``ingest()`` is the only way a photo enters Inkframe.  It never trusts the
file name or the ``Content-Type``: the format is sniffed from magic bytes and
Pillow is only allowed to open it with that one plugin.  The steps:

1. size check (``UploadTooLarge``, HTTP 413);
2. sniff JPEG / PNG / WEBP / HEIC-HEIF / TIFF / BMP / GIF from the first
   bytes (``UnsupportedImageType``, HTTP 415, for anything else);
3. scan the raw bytes for what Pillow's pure-Python header parsers would
   loop over or keep (``scan_metadata``: JPEG segments, PNG chunks and their
   text, GIF comments, EXIF, TIFF and MPF tag directories, TIFF strips) and
   count a JPEG's scans, refusing a file that would make them (or libjpeg)
   spend unbounded time or memory; then open it (headers only) and refuse
   anything over ``max_pixels``, an RLE BMP over ``MAX_RLE_BMP_PIXELS``, or an
   estimated peak memory over ``max_decode_bytes`` (``decode_cost``), all
   ``UndecodableImage``, HTTP 422, before any pixel is decoded (reading a
   PNG's EXIF would decode it, so the orientation is read afterwards).  No
   process-wide Pillow setting is changed;
4. decode small: JPEG through ``Image.draft``, so libjpeg decodes at 1/2,
   1/4 or 1/8 scale straight away, never below the master size (a 48 MP
   phone JPEG never exists at full size in memory); HEIC through
   pillow-heif's own API, libheif's buffer box-reduced into Pillow strip by
   strip (``reduce_rows``); everything else decoded by Pillow, then brought
   to 8 bits, composited on white and box-reduced to no less than the
   master size strip by strip (``normalise``), so no second full-size copy
   ever exists;
5. Lanczos downscale so the long edge is at most ``MASTER_LONG_EDGE`` (1800)
   pixels, never upscaled;
6. EXIF orientation, read from the decoded file and applied to the
   master-size image (the same table as ``ImageOps.exif_transpose``,
   without its full-size copy).  libheif applies HEIF orientation itself,
   and Pillow's TIFF plugin applies a TIFF's while loading it (and drops the
   tag), so neither is turned twice;
7. an embedded ICC profile converted to sRGB when littlecms can;
8. saved as baseline JPEG q92 without EXIF or ICC to
   ``<data>/masters/<photo_id>.jpg``, atomically.

The original file is not kept, and its name only reaches the database
(``sanitize_name``); files on disk are named by the photo id.  Everything
here is CPU- and memory-heavy and runs on the render executor, never on the
event loop.
"""

from __future__ import annotations

import io
import logging
import math
import re
import struct
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, JpegImagePlugin, PngImagePlugin

from inkframe.db import new_id
from inkframe.paths import DataPaths, atomic_write_bytes

log = logging.getLogger("inkframe.render.masters")

#: largest upload accepted (plan section 10)
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
#: largest image (width x height) Pillow is allowed to open
MAX_PIXELS = 60_000_000
#: estimated peak memory of one ingest above the process's baseline, the
#: upload's own bytes included (``decode_cost`` plus ``len(data)``).  The
#: plan's target is under 250 MB RSS: the app's baseline is about 62 MB, and
#: the one other upload ``api.photos.UPLOAD_SLOTS`` lets in meanwhile holds up
#: to 25 MB, which leaves about 160 MB; 150 keeps some slack for the Pi
MAX_DECODE_BYTES = 150_000_000
#: masters are stored with the long edge at most this (3 x the panel's 600)
MASTER_LONG_EDGE = 1800
MASTER_QUALITY = 92
#: original names are cut to this many characters before they reach the DB
MAX_NAME_CHARS = 120
#: the colour composited under transparent pixels
WHITE_RGB = (255, 255, 255)
#: an RLE-compressed BMP goes through Pillow's pure-Python decoder (about
#: 100 ns a pixel on the desktop, several times that on the Pi): a larger one
#: is refused rather than hold the one render thread for minutes
MAX_RLE_BMP_PIXELS = 4_000_000
#: steps Pillow's pure-Python header parsers may take: JPEG markers and junk
#: bytes before the first scan, PNG chunks, GIF blocks before the first
#: image.  Real files take at most a few thousand; a crafted 25 MB one could
#: take millions (minutes on the Pi) and keep an object for each (a JPEG of
#: empty APP segments holds ~120 bytes per 4 bytes of file: 750 MB)
MAX_HEADER_STEPS = 50_000
#: bytes Pillow's GIF parser may copy while it joins a comment, which it
#: does one 255-byte block at a time (quadratic: about a 360 KB comment)
MAX_GIF_COMMENT_COPY = 256_000_000
#: scans libjpeg may decode in one JPEG.  Each is another pass over the
#: image, so a crafted file of thousands of tiny progressive scans holds the
#: one render thread for minutes.  A baseline JPEG has one scan (one per
#: component when not interleaved), a progressive one about 10 (libjpeg's
#: default script) and at most about 15
MAX_JPEG_SCANS = 64
#: Huffman (DHT) and quantisation (DQT) tables in one JPEG, before and
#: between its scans; one segment can hold several.  Real files have a few,
#: or one or two per scan.  Pillow parses every DQT table before the first
#: scan in Python (25 MB of them take 0.8 s on the desktop)
MAX_JPEG_TABLES = 256
#: strips or tiles one TIFF IFD may list.  Pillow's TIFF plugin builds about
#: 320 bytes of Python objects per strip at ``Image.open`` (a tile, its
#: extents, the offset and byte count): a 24 MB TIFF of 3 million one-row
#: strips peaked at 914 MB.  A 60 MP photo stored a row a strip has under 10 000
MAX_TIFF_STRIPS = 100_000

#: ISO-BMFF major/compatible brands that mean HEVC-coded HEIF (iPhone photos)
_HEIC_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"})
#: generic HEIF structural brands; usually HEVC inside, sometimes AVIF
_HEIF_BRANDS = frozenset({b"mif1", b"msf1"})
_AVIF_BRANDS = frozenset({b"avif", b"avis"})
#: BMP DIB header sizes (OS/2 v1, BITMAPINFOHEADER, v2, v3, OS/2 v2, v4, v5)
_BMP_DIB_SIZES = frozenset({12, 40, 52, 56, 64, 108, 124})
#: a photo id: ``uuid4().hex``
_PHOTO_ID = re.compile(r"[0-9a-f]{32}")

#: Pillow plugin id for each sniffed format; ``Image.open`` may only use that one
FORMATS: dict[str, str] = {
    "jpeg": "JPEG",
    "png": "PNG",
    "webp": "WEBP",
    "heif": "HEIF",
    "tiff": "TIFF",
    "bmp": "BMP",
    "gif": "GIF",
}

#: EXIF orientation -> transpose, exactly as ``ImageOps.exif_transpose``
_ORIENTATION = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
_EXIF_ORIENTATION = 0x0112

_heif_lock = threading.Lock()
_heif_state: bool | None = None


class IngestError(ValueError):
    """An upload was refused.  ``status`` is the HTTP status the API answers with."""

    status = 422


class UploadTooLarge(IngestError):
    """More than ``max_bytes``."""

    status = 413


class UnsupportedImageType(IngestError):
    """The bytes are not one of the image formats Inkframe accepts."""

    status = 415


class UndecodableImage(IngestError):
    """A recognised format that cannot be decoded (corrupt, truncated, a
    decompression bomb, or too large to decode safely on the Pi)."""

    status = 422


@dataclass(frozen=True)
class Master:
    """A stored master: what ``db.add_photo`` needs to know about it."""

    #: the photo id; the file is ``<data>/masters/<id>.jpg``
    id: str
    #: absolute path of the JPEG
    path: Path
    width: int
    height: int
    #: taller than wide after EXIF orientation (letterboxed, never rotated)
    portrait: bool
    #: size of the stored JPEG
    bytes: int
    #: the sanitised upload name (display only)
    original_name: str
    #: the sniffed source format (``"jpeg"``, ``"heif"``, ...)
    source_format: str

    @property
    def relative_path(self) -> str:
        """The path stored in the database, relative to the data directory."""
        return master_relpath(self.id)


# --- names and paths ---------------------------------------------------------------


def valid_photo_id(photo_id: object) -> bool:
    """32 lowercase hex characters (``uuid4().hex``)."""
    return isinstance(photo_id, str) and _PHOTO_ID.fullmatch(photo_id) is not None


def _require_id(photo_id: object) -> str:
    if not valid_photo_id(photo_id):
        raise ValueError(f"not a photo id: {photo_id!r}")
    return photo_id  # type: ignore[return-value]


def master_relpath(photo_id: str) -> str:
    """``masters/<photo_id>.jpg``: what ``Photo.master_path`` holds.
    ``ValueError`` for anything but a photo id, so an id never becomes a path."""
    return f"masters/{_require_id(photo_id)}.jpg"


def master_path(paths: DataPaths, photo_id: str) -> Path:
    """Absolute path of a photo's master; ``ValueError`` for a malformed id."""
    return paths.masters / f"{_require_id(photo_id)}.jpg"


def sanitize_name(name: object) -> str:
    """A display-safe version of an upload's file name.

    Keeps only the last path component, strips control and other invisible
    format characters, collapses whitespace and cuts the result to
    ``MAX_NAME_CHARS`` (keeping a short extension).  Never empty: ``"photo"``.
    """
    text = name if isinstance(name, str) else ""
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = unicodedata.normalize("NFC", text)
    # whitespace controls (tab, newline) become spaces; every other control
    # or invisible format character (NUL, ESC, RTL override...) is dropped
    text = "".join(
        " " if ch.isspace() else ch
        for ch in text
        if ch.isspace() or not unicodedata.category(ch).startswith("C")
    )
    text = " ".join(text.split())
    if len(text) > MAX_NAME_CHARS:
        stem, dot, ext = text.rpartition(".")
        if dot and stem and 0 < len(ext) <= 10:
            text = stem[: MAX_NAME_CHARS - len(ext) - 1].rstrip() + "." + ext
        else:
            text = text[:MAX_NAME_CHARS].rstrip()
    return text or "photo"


# --- sniffing ------------------------------------------------------------------------


def _ftyp_brands(data: bytes) -> set[bytes] | None:
    """Major and compatible brands of an ISO-BMFF ``ftyp`` box, or None."""
    if len(data) < 16 or data[4:8] != b"ftyp":
        return None
    size = int.from_bytes(data[0:4], "big")
    end = min(len(data), size if 16 <= size <= 4096 else 64)
    brands = {data[8:12]}
    for off in range(16, end - 3, 4):
        brands.add(data[off : off + 4])
    return brands


def sniff_format(data: bytes) -> str:
    """The image format of ``data`` from its magic bytes: one of ``FORMATS``.

    Raises ``UnsupportedImageType`` for anything else (including AVIF and
    empty input).  Extensions and MIME types are never consulted.
    """
    if not data:
        raise UnsupportedImageType("the file is empty")
    head = bytes(data[:4096])
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:2] == b"BM" and int.from_bytes(head[14:18], "little") in _BMP_DIB_SIZES:
        return "bmp"
    brands = _ftyp_brands(head)
    if brands is not None:
        if brands & _HEIC_BRANDS:
            return "heif"
        if brands & _AVIF_BRANDS:
            raise UnsupportedImageType("AVIF images are not supported; upload a JPEG or HEIC")
        if brands & _HEIF_BRANDS:
            return "heif"
    raise UnsupportedImageType(
        "not a supported image: upload a JPEG, PNG, WEBP, HEIC, TIFF, BMP or GIF"
    )


def heif_available() -> bool:
    """Register pillow-heif's opener once; False if pillow-heif is unusable."""
    global _heif_state
    if _heif_state is None:
        with _heif_lock:
            if _heif_state is None:
                try:
                    import pillow_heif

                    # depth maps, auxiliary images and thumbnails are never used
                    pillow_heif.register_heif_opener(
                        thumbnails=False, depth_images=False, aux_images=False
                    )
                    _heif_state = True
                except Exception:  # ImportError, or a broken native wheel
                    log.exception("pillow-heif is unavailable; HEIC uploads will be refused")
                    _heif_state = False
    return _heif_state


# --- header structure -------------------------------------------------------------------

#: what Pillow keeps per header item besides its payload: the key, the bytes
#: or str object, dict and list slots (measured: ~160 bytes a PNG text chunk)
_ITEM_OVERHEAD = 256
#: zlib cannot expand its input more than about 1032 times
_ZLIB_MAX_RATIO = 1032
#: PNG chunks Pillow decodes into ``info`` / ``text``
_PNG_METADATA = frozenset({b"tEXt", b"zTXt", b"iTXt", b"iCCP", b"eXIf"})
#: text chunks Pillow may keep twice: as bytes in ``info`` and as a str in
#: ``text`` (``tEXt`` keyed "exif", ``iTXt`` keyed XMP)
_PNG_TWO_COPIES = frozenset({b"tEXt", b"iTXt"})
#: chunks with a lowercase second letter that Pillow decodes itself (APNG);
#: it keeps any other such "private" chunk whole in ``private_chunks``
_PNG_APNG = frozenset({b"acTL", b"fcTL", b"fdAT"})
_NON_ASCII = re.compile(rb"[\x80-\xff]")
_GIF_EXTENSION, _GIF_IMAGE, _GIF_TRAILER, _GIF_COMMENT = 0x21, 0x2C, 0x3B, 0xFE

#: size in bytes of each TIFF 6 / EXIF field type (1..13); an unknown type is
#: treated as one byte, exactly as Pillow's ``load`` ignores it but we must
#: not under-count.  16/17/18 are BigTIFF's 8-byte LONG8/SLONG8/IFD8.
_IFD_TYPE_SIZE = {
    1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4,
    16: 8, 17: 8, 18: 8,
}  # fmt: skip
#: IFD pointer tags whose sub-IFD Pillow can walk and materialise (``getexif``
#: ``get_ifd`` / ``_get_merged_dict``): Exif, GPS and Interop
_IFD_SUB_TAGS = frozenset({0x8769, 0x8825, 0xA005})
#: no legitimate EXIF/TIFF IFD comes near these; a crafted one has thousands
#: of entries, or a cycle of IFDs, all pointing at the same large region
_MAX_IFD_ENTRIES = 2000
_MAX_IFDS = 32
#: TIFF tags that hold one value per strip or tile: StripOffsets,
#: StripByteCounts, TileOffsets, TileByteCounts
_TIFF_STRIP_TAGS = frozenset({0x0111, 0x0117, 0x0144, 0x0145})
#: what Pillow's TIFF plugin builds per strip or tile at ``Image.open``, on
#: top of the raw tag data the IFD walk charges (measured: 316 to 324 bytes
#: a strip, its raw offset and byte count included)
_TIFF_STRIP_BYTES = 320
#: Pillow's MPF parser (``JpegImagePlugin._getmp``, inside ``Image.open``)
#: turns the whole MP index into Python objects at once (``dict(ifd)``),
#: where ``getexif`` keeps each tag's raw bytes until it is read.  Per value,
#: raw bytes included (measured: 38 to 44 bytes for a SHORT, LONG, FLOAT or
#: DOUBLE, a tuple slot and an int or float; about 245 for a RATIONAL, an
#: ``IFDRational`` and its ``Fraction``; about 2.4 for BYTE and ASCII, copied
#: as bytes or str); UNDEFINED values stay one byte each
_IFD_OBJECT_BYTES = {
    1: 3, 2: 3, 3: 48, 4: 48, 5: 256, 6: 48, 8: 48, 9: 48, 10: 256, 11: 48, 12: 48, 13: 48, 16: 48,
}  # fmt: skip
#: and per entry: its dict slots and tag lookup (measured: about 440 bytes)
_IFD_ENTRY_BYTES = 512
#: the MP Entry tag, 16 bytes per image, which ``_getmp`` expands into two
#: dicts per image (measured: about 680 bytes an entry)
_MPF_ENTRY_TAG = 0xB002
_MPF_ENTRY_BYTES = 768
#: a marker in or after a JPEG's entropy-coded data: 0xFF and a code that is
#: not a stuffed zero, a restart marker (part of the scan) or a fill byte
_JPEG_MARKER = re.compile(rb"\xff[\x01-\xcf\xd8-\xfe]")
_JPEG_TABLE_MARKERS = frozenset({0xFFC4, 0xFFDB})  # DHT, DQT
#: markers without a length that libjpeg steps over (TEM) or stops at (SOI)
#: inside the scans
_JPEG_STANDALONE = frozenset({0xFF01, 0xFFD8})


@dataclass(frozen=True)
class Metadata:
    """What Pillow's header parser keeps of a file besides its pixels, in
    bytes (``scan_metadata``)."""

    #: alive from ``Image.open`` until the master is encoded (images derived
    #: from the opened one share it through their copied ``info``)
    retained: int = 0
    #: alive besides that while the largest item is read (the raw chunk and
    #: Pillow's copies of it); text after a PNG's pixels is read during the decode
    transient: int = 0


def _check_steps(steps: int, what: str) -> None:
    if steps > MAX_HEADER_STEPS:
        raise UndecodableImage(
            f"the file has more than {MAX_HEADER_STEPS} {what}, more than this frame"
            " parses; re-save it as a plain JPEG or PNG"
        )


@dataclass(frozen=True)
class _JpegHeaders:
    """What ``_walk_jpeg`` found before a JPEG's first scan."""

    #: steps Pillow's parser takes over them (``MAX_HEADER_STEPS``)
    steps: int = 0
    #: the APP and COM payloads Pillow keeps
    retained: int = 0
    #: DHT and DQT tables (``MAX_JPEG_TABLES``)
    tables: int = 0
    #: the EXIF block Pillow joins from the APP1 ``Exif\0\0`` segments
    exif: bytes = b""
    #: the MP index (the TIFF block after ``MPF\0``) of every APP2 MPF segment
    mpf: tuple[bytes, ...] = ()
    #: where the first SOS marker is; -1 when there is none
    sos: int = -1


def _count_jpeg_tables(data: bytes, marker: int, body: int, stop: int, tables: int) -> int:
    """``tables`` plus the tables in one DHT or DQT segment (its payload is
    ``data[body:stop]``; it can hold several, and counts as at least one).
    Raises ``UndecodableImage`` past ``MAX_JPEG_TABLES``."""
    pos = body
    while True:
        tables += 1
        if tables > MAX_JPEG_TABLES:
            raise UndecodableImage(
                f"the JPEG has more than {MAX_JPEG_TABLES} Huffman and quantisation tables,"
                " more than this frame parses; re-save it as a plain JPEG"
            )
        if pos < stop:
            if marker == 0xFFDB:  # DQT: precision and id, then 64 one- or two-byte values
                pos += 1 + (128 if data[pos] >> 4 else 64)
            else:  # DHT: class and id, 16 code-length counts, then that many symbols
                pos += 17 + sum(data[pos + 1 : pos + 17])
        if pos >= stop:
            return tables


def _walk_jpeg(data: bytes) -> _JpegHeaders:
    """Walk the markers before the first scan the way Pillow's parser does:
    one step per marker, fill byte and junk byte (it reads junk a byte at a
    time and goes on parsing after it), the APP and COM payloads it keeps
    (``applist`` plus a derived copy: EXIF, XMP, the joined ICC profile), the
    EXIF and MPF blocks it parses (``getexif`` and ``_getmp``) and the DHT and
    DQT tables.  Raises ``UndecodableImage`` past ``MAX_HEADER_STEPS`` or
    ``MAX_JPEG_TABLES``."""
    pos, end = 2, len(data)  # Pillow starts at the marker after SOI
    steps = retained = tables = 0
    exif: list[bytes] = []
    mpf: list[bytes] = []
    sos = -1
    while pos + 1 < end:
        steps += 1
        if data[pos] != 0xFF:  # junk: stepped over one byte at a time
            nxt = data.find(b"\xff", pos)
            if nxt < 0:
                break
            steps += nxt - pos - 1
            pos = nxt
        elif data[pos + 1] == 0xFF:  # a fill byte
            pos += 1
        elif data[pos + 1] == 0x00:  # an escaped 0xFF
            pos += 2
        else:
            marker = 0xFF00 | data[pos + 1]
            entry = JpegImagePlugin.MARKER.get(marker)
            if entry is None:  # Pillow refuses the file
                break
            if marker == 0xFFDA:  # the first scan: Pillow stops here, libjpeg goes on
                sos = pos
                break
            if entry[2] is None:  # SOI, EOI, RSTn...: no length
                pos += 2
            else:
                length = int.from_bytes(data[pos + 2 : pos + 4], "big")
                body, stop = pos + 4, min(end, pos + 2 + length)
                if 0xFFE0 <= marker <= 0xFFEF or marker == 0xFFFE:
                    retained += 2 * max(0, length - 2) + _ITEM_OVERHEAD
                    if marker == 0xFFE1 and data.startswith(b"Exif\x00\x00", body, stop):
                        # the first whole, later ones appended without the prefix
                        # (``self.info["exif"] += s[6:]``)
                        exif.append(data[body + (6 if exif else 0) : stop])
                    elif marker == 0xFFE2 and data.startswith(b"MPF\x00", body, stop):
                        mpf.append(data[body + 4 : stop])
                elif marker in _JPEG_TABLE_MARKERS:
                    tables = _count_jpeg_tables(data, marker, body, stop, tables)
                pos += 2 + max(2, length)  # a length below 2: Pillow reads no payload
        _check_steps(steps, "JPEG header markers and padding bytes")
    return _JpegHeaders(steps, retained, tables, b"".join(exif), tuple(mpf), sos)


def _check_jpeg_scans(data: bytes, head: _JpegHeaders) -> None:
    """Count the scans libjpeg decodes, from the first SOS up to EOI (an
    MPO's other images follow it), and the DHT/DQT tables between them.
    Raises ``UndecodableImage`` past ``MAX_JPEG_SCANS`` or
    ``MAX_JPEG_TABLES``, or past ``MAX_HEADER_STEPS`` markers in all.

    Jumps from marker to marker over the entropy-coded data at C speed
    (``_JPEG_MARKER``: its stuffed zeros, restart markers and fill bytes are
    not markers), stepping over marker segments by their length."""
    pos, end = head.sos, len(data)
    if pos < 0:
        return
    steps, tables, scans = head.steps, head.tables, 0
    while pos + 1 < end:
        marker = 0xFF00 | data[pos + 1]
        if marker == 0xFFD9:  # EOI
            return
        steps += 1
        _check_steps(steps, "JPEG markers")
        if marker in _JPEG_STANDALONE:
            nxt = pos + 2
        else:
            length = int.from_bytes(data[pos + 2 : pos + 4], "big")
            if marker == 0xFFDA:
                scans += 1
                if scans > MAX_JPEG_SCANS:
                    raise UndecodableImage(
                        f"the JPEG has more than {MAX_JPEG_SCANS} scans (a progressive JPEG has"
                        " about 10); decoding it would hold this frame for too long. Re-save it"
                        " as a baseline JPEG"
                    )
            elif marker in _JPEG_TABLE_MARKERS:
                stop = min(end, pos + 2 + length)
                tables = _count_jpeg_tables(data, marker, pos + 4, stop, tables)
            nxt = pos + 2 + max(2, length)
        found = _JPEG_MARKER.search(data, nxt)
        if found is None:
            return
        pos = found.start()


def _scan_jpeg(data: bytes) -> Metadata:
    """A JPEG's headers (``_walk_jpeg``) and scans (``_check_jpeg_scans``),
    plus what Pillow materialises from its EXIF block and its MP index
    inside ``Image.open``: ``getexif`` keeps the raw tag data
    (``_read_dpi_from_exif``), ``_getmp`` turns every value into a Python
    object (``objects``), and is parsed for every JPEG, not only MPOs."""
    head = _walk_jpeg(data)
    _check_jpeg_scans(data, head)
    meta = _with_ifd(Metadata(retained=head.retained), head.exif)
    for block in head.mpf:  # Pillow parses the last one; every one is charged
        meta = _with_ifd(meta, block, objects=True)
    return meta


def _png_text_bytes(data: bytes, kind: bytes, body: int, stop: int) -> int:
    """An upper bound of what Pillow stores for one metadata chunk (its
    payload is ``data[body:stop]``): the decompressed text or profile, as a
    ``str`` for text (Latin-1 one byte a character, UTF-8 up to four)."""
    n = stop - body
    inflated = min(PngImagePlugin.MAX_TEXT_CHUNK, n * _ZLIB_MAX_RATIO)
    if kind in (b"zTXt", b"iCCP"):
        return inflated
    if kind == b"iTXt":
        nul = data.find(b"\0", body, stop)  # the keyword ends; the compression flag follows
        if 0 <= nul < stop - 1 and data[nul + 1] != 0:
            return 4 * inflated
        return 4 * n if _NON_ASCII.search(data, body, stop) else n
    return n  # tEXt, eXIf


def _png_read_bytes(kind: bytes, n: int, kept: int) -> int:
    """What Pillow holds for a moment, besides what it keeps, while it reads
    one ``n``-byte metadata chunk: the chunk and its slices (two for most,
    four for iTXt's several fields), and the inflated bytes or the decode
    buffer of a compressed or UTF-8 chunk (measured: 2n for an 8 MB tEXt,
    3n for a 2 MB iTXt, 1 MB for a zTXt that inflates to 1 MB)."""
    if kind == b"iTXt":
        return 4 * n + kept
    if kind in (b"zTXt", b"iCCP"):
        return 2 * n + 2 * kept
    return 2 * n  # tEXt, eXIf


def _scan_png(data: bytes) -> Metadata:
    """Walk every chunk up to IEND: one step each (Pillow handles each in
    Python, at open or while loading), counting the text, ICC and EXIF it
    decodes into ``info`` and ``text`` and the private chunks it keeps
    whole."""
    pos, end = 8, len(data)
    steps = retained = transient = 0
    while pos + 8 <= end:
        steps += 1
        _check_steps(steps, "PNG chunks")
        length = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        if kind == b"IEND":
            break
        body = pos + 8
        stop = min(end, body + length)
        if kind in _PNG_METADATA:
            kept = _png_text_bytes(data, kind, body, stop)
            copies = 2 if kind in _PNG_TWO_COPIES else 1
            retained += copies * kept + _ITEM_OVERHEAD
            transient = max(transient, _png_read_bytes(kind, stop - body, kept))
        elif kind[1:2].islower() and kind not in _PNG_APNG:
            retained += stop - body + _ITEM_OVERHEAD
        pos = body + length + 4
    return Metadata(retained=retained, transient=transient)


def _scan_gif(data: bytes) -> Metadata:
    """Walk the blocks before the first image the way Pillow's parser does
    (one step per extension, sub-block and junk byte) and count the bytes it
    copies while joining comments: it appends each 255-byte block to the
    comment so far, so a long comment costs quadratic time."""
    end = len(data)
    if end < 13:
        return Metadata()
    flags = data[10]
    pos = 13 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0)  # after the global palette
    steps = copied = 0
    joined: int | None = None  # the comment so far, once there is one (even empty)
    while pos < end:
        steps += 1
        _check_steps(steps, "GIF header blocks")
        intro = data[pos]
        if intro in (_GIF_IMAGE, _GIF_TRAILER):
            break
        if intro != _GIF_EXTENSION:  # junk: stepped over one byte at a time
            pos += 1
            continue
        label = data[pos + 1] if pos + 1 < end else 0
        pos += 2
        comment = 0
        while pos < end:
            size = data[pos]
            pos += 1 + size
            steps += 1
            if size == 0:
                break
            if label == _GIF_COMMENT:
                copied += comment + size  # ``comment += block``
                comment += size
        _check_steps(steps, "GIF header blocks")
        if label == _GIF_COMMENT:
            if joined is None:
                joined = comment
            else:  # ``info["comment"] += b"\n" + comment``
                copied += joined + 2 * (1 + comment)
                joined += 1 + comment
            if copied > MAX_GIF_COMMENT_COPY:
                raise UndecodableImage(
                    "the GIF's comments are too long for this frame to parse; re-save it"
                    " without them"
                )
    return Metadata(retained=joined or 0)


def _walk_ifds(tiff: bytes, *, objects: bool = False) -> tuple[int, int]:
    """Walk the IFD tree of an EXIF/TIFF block (``tiff`` starts at the TIFF
    header, after any ``Exif\\0\\0`` prefix) and return the bytes Pillow
    would materialise from it, and the most strips or tiles any IFD lists
    (the largest count of a ``_TIFF_STRIP_TAGS`` tag).

    Pillow's ``ImageFileDirectory_v2`` reads every entry's
    ``count x type_size`` value into ``_tagdata``, so a single IFD of
    thousands of entries that all point at the same large region expands to
    ``entries x region`` bytes in memory from a tiny file.  ``getexif()``
    keeps those raw bytes and unpacks a tag only when it is read; with
    ``objects`` every value is charged as the Python object it becomes
    (``_IFD_OBJECT_BYTES``, ``_IFD_ENTRY_BYTES``, ``_MPF_ENTRY_BYTES``), as
    Pillow's MPF parser unpacks them all.

    Walks IFD0, the next-IFD chain and the sub-IFDs reachable through the
    Exif (0x8769), GPS (0x8825) and Interop (0xA005) pointer tags, with a
    visited set (no offset twice), a cap of ``_MAX_IFDS`` IFDs and
    ``_MAX_IFD_ENTRIES`` entries per IFD.  BigTIFF (magic 43) is handled too.

    Raises ``UndecodableImage`` when an IFD has more than ``_MAX_IFD_ENTRIES``
    entries or the walk would touch more than ``_MAX_IFDS`` IFDs; no genuine
    file does, and both are reached long before the memory would be.  A
    malformed offset (out of range) is not an error: that branch stops.
    """
    if tiff[:6] == b"Exif\x00\x00":
        tiff = tiff[6:]
    end = len(tiff)
    order = tiff[:2]
    if end < 8 or order not in (b"II", b"MM"):
        return 0, 0
    endian = "<" if order == b"II" else ">"
    magic = struct.unpack(endian + "H", tiff[2:4])[0]
    # ``dir_fmt``: the IFD's entry-count field (2 bytes classic, 8 BigTIFF).
    # ``head_fmt``/``head_sz``: tag, type and value-count of one entry (the
    # count is 4 bytes classic, 8 BigTIFF).  ``off_fmt``/``off_sz``: the
    # value/offset field that follows (4 bytes classic, 8 BigTIFF).
    if magic == 43:  # BigTIFF
        dir_sz, dir_fmt, head_sz, head_fmt, off_sz, off_fmt, entry_sz = (
            8,
            "Q",
            12,
            "HHQ",
            8,
            "Q",
            20,
        )
        if end < 16:
            return 0, 0
        first = struct.unpack(endian + "Q", tiff[8:16])[0]
    elif magic == 42:  # classic TIFF
        dir_sz, dir_fmt, head_sz, head_fmt, off_sz, off_fmt, entry_sz = 2, "H", 8, "HHI", 4, "I", 12
        first = struct.unpack(endian + "I", tiff[4:8])[0]
    else:
        return 0, 0

    total = strips = 0
    visited: set[int] = set()
    stack = [first]
    while stack:
        off = stack.pop()
        if off in visited or off < 8 or off + dir_sz > end:
            continue  # out of range: a malformed branch, stop walking it
        visited.add(off)
        if len(visited) > _MAX_IFDS:
            raise UndecodableImage(
                "the file's embedded metadata chains through too many IFDs; its EXIF is"
                " too large to decode safely. Re-save it without the metadata"
            )
        count = struct.unpack(endian + dir_fmt, tiff[off : off + dir_sz])[0]
        if count > _MAX_IFD_ENTRIES:
            raise UndecodableImage(
                f"the file has an EXIF/TIFF IFD of {count} tags (more than {_MAX_IFD_ENTRIES});"
                " its metadata is too large to decode safely. Re-save it without the metadata"
            )
        entries = off + dir_sz
        last = entries + entry_sz * count
        pos = entries
        while pos + entry_sz <= end and pos < last:
            tag, typ, cnt = struct.unpack(endian + head_fmt, tiff[pos : pos + head_sz])
            size = _IFD_TYPE_SIZE.get(typ, 1)
            if objects:
                total += _IFD_ENTRY_BYTES + cnt * _IFD_OBJECT_BYTES.get(typ, size)
                if tag == _MPF_ENTRY_TAG:
                    total += cnt * size // 16 * _MPF_ENTRY_BYTES
            else:
                total += cnt * size
            if tag in _TIFF_STRIP_TAGS:
                strips = max(strips, cnt)
            if tag in _IFD_SUB_TAGS:  # the value field holds a sub-IFD offset
                sub = struct.unpack(endian + off_fmt, tiff[pos + head_sz : pos + head_sz + off_sz])[
                    0
                ]
                if sub not in visited:
                    stack.append(sub)
            pos += entry_sz
        if last + off_sz <= end:  # the next IFD in the chain (IFD1, the thumbnail)
            nxt = struct.unpack(endian + off_fmt, tiff[last : last + off_sz])[0]
            if nxt and nxt not in visited:
                stack.append(nxt)
    return total, strips


def _ifd_materialised_bytes(tiff: bytes, *, objects: bool = False) -> int:
    """The bytes Pillow would materialise from the IFD tree of an EXIF/TIFF
    block (``_walk_ifds``, which raises ``UndecodableImage`` for a crafted
    one); with ``objects``, as Pillow's MPF parser unpacks every value."""
    return _walk_ifds(tiff, objects=objects)[0]


def _scan_tiff(data: bytes) -> Metadata:
    """A TIFF file's own IFDs (``_walk_ifds``).  Pillow reads IFD0's tag data
    twice, into ``tag_v2`` at ``Image.open`` and again for ``getexif`` while
    it loads the pixels (measured: 8 MB of tags held 16 MB), and builds
    Python objects per strip or tile at ``Image.open``
    (``_TIFF_STRIP_BYTES``).  Raises ``UndecodableImage`` for an IFD of more
    than ``MAX_TIFF_STRIPS`` strips or tiles."""
    materialised, strips = _walk_ifds(data)
    if strips > MAX_TIFF_STRIPS:
        raise UndecodableImage(
            f"the TIFF is stored in {strips} strips or tiles (more than {MAX_TIFF_STRIPS}),"
            " more than this frame parses safely; re-save it with larger strips, or as a"
            " PNG or JPEG"
        )
    return Metadata(retained=2 * materialised + strips * _TIFF_STRIP_BYTES)


def _jpeg_exif_block(data: bytes) -> bytes:
    """The EXIF TIFF block Pillow builds from a JPEG's APP1 ``Exif\\0\\0``
    segments: the first whole, every later one appended without its 6-byte
    prefix (``self.info["exif"] += s[6:]``), exactly as ``JpegImageFile``
    concatenates them, found by walking the markers as Pillow does
    (``_walk_jpeg``: junk bytes do not end the walk).  Empty when there is
    none."""
    return _walk_jpeg(data).exif


def _png_exif_blocks(data: bytes) -> list[bytes]:
    """The payloads of every PNG ``eXIf`` chunk (the raw EXIF TIFF block
    Pillow stores in ``info["exif"]``; Pillow keeps the LAST one it meets,
    including chunks after IDAT read in ``load_end``, so every chunk is
    charged rather than guessing which one wins).  Walks chunk lengths only."""
    blocks: list[bytes] = []
    pos, end = 8, len(data)
    while pos + 8 <= end:
        length = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        if kind == b"IEND":
            break
        if kind == b"eXIf":
            blocks.append(bytes(data[pos + 8 : min(end, pos + 8 + length)]))
        pos += 12 + length
    return blocks


def _webp_exif_block(data: bytes) -> bytes:
    """The payload of a WEBP ``EXIF`` RIFF chunk (what libwebp hands Pillow as
    ``info["exif"]``), or empty.  Walks chunk sizes only."""
    end = len(data)
    if end < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return b""
    pos = 12
    while pos + 8 <= end:
        fourcc = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        body = pos + 8
        if fourcc == b"EXIF":
            return bytes(data[body : min(end, body + size)])
        pos = body + size + (size & 1)  # chunks are padded to an even size
    return b""


def _with_ifd(meta: Metadata, exif: bytes, *, objects: bool = False) -> Metadata:
    """``meta`` with what ``getexif()`` would materialise from ``exif`` added
    to its retained bytes: it is allocated inside (JPEG) or right after
    ``Image.open`` and stays alive on ``im._exif`` until the master is
    encoded, the same lifetime as the rest of ``retained``.  With
    ``objects``, every value as the Python object it becomes (a JPEG's MP
    index, which an MPO keeps as ``mpinfo``)."""
    extra = _ifd_materialised_bytes(exif, objects=objects) if exif else 0
    if not extra:
        return meta
    return Metadata(retained=meta.retained + extra, transient=meta.transient)


def scan_metadata(data: bytes, source_format: str) -> Metadata:
    """What Pillow's header parser will keep of ``data`` (sniffed as
    ``source_format``), from the raw bytes, before Pillow parses anything.

    Pillow parses JPEG markers, PNG chunks and GIF blocks in Python, one at
    a time, and keeps an object for many of them; a crafted file can make
    that take minutes and hundreds of MB inside ``Image.open``, before any
    budget check could run.  Raises ``UndecodableImage`` when a file would
    take more than ``MAX_HEADER_STEPS`` steps or, for GIF, copy more than
    ``MAX_GIF_COMMENT_COPY`` bytes of comments.  libjpeg decodes a JPEG's
    scans in C, but each is a pass over the whole image: a JPEG with more
    than ``MAX_JPEG_SCANS`` scans or ``MAX_JPEG_TABLES`` DHT/DQT tables is
    refused too (``_check_jpeg_scans``).

    It also walks any embedded EXIF/TIFF IFD tree (``_walk_ifds``) and counts
    what Pillow would materialise from it: a ~100 KB file whose IFD holds
    thousands of tags all pointing at one large region makes Pillow allocate
    ``tags x region`` bytes (hundreds of MB) when it parses the IFD -- inside
    ``Image.open`` for JPEG (``_read_dpi_from_exif``, and ``_getmp`` for its
    MPF segment, which unpacks every value into a Python object), in
    ``_orientation`` for PNG/WEBP, and while the file's own IFD is parsed for
    TIFF (twice, and with ~320 bytes of objects per strip or tile:
    ``_scan_tiff``, which refuses more than ``MAX_TIFF_STRIPS``).
    ``decode_cost`` never saw it, because this scan only sized the raw
    segment/chunk.  Charged as retained bytes, it is refused by the same
    budget check that runs before ``Image.open`` (``_decode_pillow``).
    """
    if source_format == "jpeg":
        return _scan_jpeg(data)
    if source_format == "png":
        meta = _scan_png(data)
        for block in _png_exif_blocks(data):
            meta = _with_ifd(meta, block)
        return meta
    if source_format == "webp":
        return _with_ifd(Metadata(), _webp_exif_block(data))
    if source_format == "tiff":
        return _scan_tiff(data)  # the file itself is the TIFF
    if source_format == "gif":
        return _scan_gif(data)
    return Metadata()


# --- decoding ---------------------------------------------------------------------------

#: bytes per pixel Pillow allocates for each mode (RGB, RGBA, LA, CMYK, I and F use 4)
_MODE_BYTES = {"1": 1, "L": 1, "P": 1, "I;16": 2, "I;16L": 2, "I;16B": 2, "I;16N": 2}
#: modes ``normalise`` turns into L (1 byte per pixel); every other mode becomes RGB or CMYK (4)
_GREY_MODES = frozenset({"1", "L", "LA", "La", "I", "F", "I;16", "I;16L", "I;16B", "I;16N"})
#: rows of a raw HEIF buffer are handed to Pillow in strips of about this size
_STRIP_BYTES = 8 * 1024 * 1024
#: ``normalise`` converts this many pixels at a time (about 2 MB per temporary)
_STRIP_PIXELS = 1 << 19
#: what a strip of ``normalise`` holds at most: the crop, its conversion, the
#: composited copy, the alpha mask (4 + 4 + 4 + 1 bytes per pixel)
_STRIP_TEMP_BYTES = 13 * _STRIP_PIXELS

# Decoder state besides Pillow's image, in bytes per decoded pixel.  Pillow
# decodes WebP through libwebp's WebPAnimDecoder: two RGBA canvases, the
# frame copied into a ``bytes`` object, then Pillow's image; the canvases
# stay alive until the image object is dropped.
_WEBP_DECODER_PER_PIXEL = 13
_WEBP_RETAINED_PER_PIXEL = 8
#: libheif's YCbCr planes next to its interleaved RGB output, and the HEVC
#: decoder's own state (measured: 24 MP peaks at +120 MB, 36 MP at +179 MB)
_HEIF_PLANES_PER_PIXEL = 1.5
_HEIF_DECODER_FIXED = 16_000_000
#: per master pixel: the transposed copy, the sRGB conversion and the JPEG
#: encoder (Pillow's output buffer and libjpeg's coefficients for
#: ``optimize``), about 10 bytes at most at once
_TAIL_PER_PIXEL = 10
# Over the modelled buffers: allocator slack, codec working memory and Python
# objects.  Calibrated against the peaks measured on the desktop (the table
# in docs/PIPELINE.md); every one of them is below its estimate.
_COST_MARGIN = 1.1
_COST_FIXED = 6_000_000

#: JPEG frame types decoded in a single sequential pass (baseline, extended,
#: arithmetic sequential); any other (progressive, lossless, hierarchical)
#: makes libjpeg keep the whole image's DCT coefficients
_SEQUENTIAL_SOF = frozenset({0xC0, 0xC1, 0xC9})
_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_TIFF_IMAGE_WIDTH = 256
_TIFF_IMAGE_LENGTH = 257
_TIFF_BITS_PER_SAMPLE = 258
_TIFF_COMPRESSION = 259
_TIFF_SAMPLES_PER_PIXEL = 277
_TIFF_ROWS_PER_STRIP = 278
_TIFF_TILE_WIDTH = 322
_TIFF_TILE_LENGTH = 323
_BMP_RLE = (1, 2)  # BI_RLE8, BI_RLE4


def _reduce_factor(size: tuple[int, int], long_edge: int) -> int:
    """The integer box reduction applied to a decoded image: as large as
    possible while keeping at least ``long_edge`` pixels on the long edge
    (1 = none).  A JPEG's draft stops at the same place."""
    return max(1, max(size) // long_edge)


def _mode_bytes(mode: str) -> int:
    return _MODE_BYTES.get(mode, 4)


def _needs_conversion(mode: str, transparency: bool) -> bool:
    """Whether ``normalise`` has to convert ``mode`` (to 8 bits, or alpha onto white)."""
    if mode in ("L", "RGB"):
        return transparency
    return mode != "CMYK"


def decode_cost(
    mode: str,
    size: tuple[int, int],
    source_format: str,
    long_edge: int = MASTER_LONG_EDGE,
    *,
    decoder_bytes: int = 0,
    retained_bytes: int = 0,
    transparency: bool = False,
) -> int:
    """Estimated peak bytes, above the process's baseline, of decoding an
    image of ``size`` (as decoded: after a JPEG's draft) in ``mode`` and
    making its master.  The upload's own bytes are not included.

    The peak is the largest of these stages, each counting what is alive at
    once (``R`` is the box-reduced image, ``M`` the master-size one):

    * **decode**: Pillow's image plus the decoder's state: ``decoder_bytes``
      read from the file's headers (``decoder_overhead()``: a progressive
      JPEG's coefficients, a TIFF's strip buffer...) and WebP's canvases;
    * **normalise**: the decoded image plus ``R``, built strip by strip
      (with one strip's temporaries) when the mode needs converting;
    * **resize**: ``R``, Lanczos's intermediate (``M``'s width by ``R``'s
      height) and ``M``;
    * **tail**: ``M`` transposed, converted to sRGB and JPEG-encoded.

    ``retained_bytes`` is alive through every stage: what the header parser
    kept (``scan_metadata``: a PNG's text, a JPEG's APP segments).

    HEIF is decoded by libheif, which holds its YCbCr planes next to its
    ``len(mode)``-byte RGB output; Pillow only receives that box-reduced,
    strip by strip (``reduce_rows``).

    Each stage is modelled from the buffers the code allocates, and the
    total is scaled by ``_COST_MARGIN`` plus ``_COST_FIXED``; every peak
    measured on the desktop is below its estimate (docs/PIPELINE.md).
    """
    w, h = size
    pixels = w * h
    factor = _reduce_factor(size, long_edge)
    rw, rh = -(-w // factor), -(-h // factor)
    reduced = rw * rh
    out = 1 if mode in _GREY_MODES else 4
    if source_format == "heif":
        bands = len(mode)
        stages = [
            pixels * (bands + _HEIF_PLANES_PER_PIXEL) + _HEIF_DECODER_FIXED,
            pixels * bands + reduced * 4 + (2 * _STRIP_BYTES if factor >= 2 else 0),
        ]
        if mode in ("RGBA", "LA"):
            stages.append(reduced * 9)  # composited on white: R, its copy and the mask
    else:
        held = pixels * _mode_bytes(mode)
        webp = source_format == "webp"
        stages = [held + decoder_bytes + (pixels * _WEBP_DECODER_PER_PIXEL if webp else 0)]
        if webp:
            held += pixels * _WEBP_RETAINED_PER_PIXEL
        if _needs_conversion(mode, transparency):
            stages.append(held + reduced * out + _STRIP_TEMP_BYTES)
        elif factor >= 2 or webp:  # a reduced copy, or WebP's copy (so its decoder can go)
            stages.append(held + reduced * out)
    if max(rw, rh) > long_edge:
        mw, mh = _scaled_size((rw, rh), long_edge)
        stages.append((reduced + mw * rh + mw * mh) * out)
    else:
        mw, mh = rw, rh
    stages.append(mw * mh * _TAIL_PER_PIXEL)
    return round((max(stages) + retained_bytes) * _COST_MARGIN) + _COST_FIXED


def _ceil_div(value: int, step: int) -> int:
    return -(-value // step)


def _round_up(value: int, step: int) -> int:
    return _ceil_div(value, step) * step


def _jpeg_buffers_coefficients(data: bytes) -> bool:
    """Whether libjpeg keeps every DCT coefficient of the image while it
    decodes: a progressive (or lossless, or hierarchical) frame, or a
    sequential one whose first scan does not carry every component (a
    non-interleaved file has more scans to come).  ``draft`` does not shrink
    that buffer.  Walks the marker segments up to the first SOS; True when
    the headers do not parse."""
    pos, end = 2, len(data)
    components = 0
    sequential = False
    while pos + 4 < end:
        if data[pos] != 0xFF:
            return True
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # standalone markers
            pos += 2
            continue
        if marker in _SOF_MARKERS:
            sequential = marker in _SEQUENTIAL_SOF
            components = data[pos + 9] if pos + 9 < end else 0
        elif marker == 0xDA:
            return not sequential or data[pos + 4] < components
        pos += 2 + int.from_bytes(data[pos + 2 : pos + 4], "big")
    return True


def _jpeg_coefficient_bytes(size: tuple[int, int], layer: list[tuple[int, int, int, int]]) -> int:
    """The size of libjpeg's whole-image coefficient buffer: 64 two-byte
    coefficients per 8 x 8 block of every component, at the component's own
    sampling (``layer`` is Pillow's ``(id, h, v, table)`` per component)."""
    w, h = size
    if not layer:
        return w * h * 3 * 2
    hmax = max(1, *(c[1] for c in layer))
    vmax = max(1, *(c[2] for c in layer))
    total = 0
    for _cid, hs, vs, _table in layer:
        hs, vs = max(1, hs), max(1, vs)
        blocks_w = _round_up(_ceil_div(_ceil_div(w * hs, hmax), 8), hs)
        blocks_h = _round_up(_ceil_div(_ceil_div(h * vs, vmax), 8), vs)
        total += blocks_w * blocks_h * 64 * 2
    return total


def decoder_overhead(im: Image.Image, data: bytes) -> int:
    """What the decoder of the opened (not loaded) ``im`` holds besides
    Pillow's image while it decodes, from the headers alone:

    * a JPEG that libjpeg must buffer whole (``_jpeg_buffers_coefficients``):
      its coefficients at full size, whatever the draft.  That includes an
      MPO: Pillow's JPEG plugin opens a JPEG whose MPF segment lists more
      than one image (phone photos with a gain map or preview) as
      ``MpoImageFile``, format ``"MPO"``, and decodes its primary image with
      the same libjpeg path;
    * a compressed TIFF: libtiff's decoded strip or tile (a whole-image
      strip is legal), and any TIFF with an orientation tag a second
      full-size image, because Pillow's TIFF plugin transposes the loaded
      image in place;
    * an RLE BMP: Pillow's Python decoder builds the pixels in a
      ``bytearray`` and copies them once.
    """
    w, h = im.size
    if isinstance(im, JpegImagePlugin.JpegImageFile):  # JPEG and MPO
        if "progressive" in im.info or _jpeg_buffers_coefficients(data):
            return _jpeg_coefficient_bytes((w, h), getattr(im, "layer", []))
        return 0
    if im.format == "TIFF":
        tags = getattr(im, "tag_v2", {})
        extra = 0
        if tags.get(_TIFF_COMPRESSION, 1) != 1:  # uncompressed: Pillow's raw decoder, no buffer
            try:
                bits = tags.get(_TIFF_BITS_PER_SAMPLE, 8)
                bits = max(bits, default=8) if isinstance(bits, tuple) else bits
                samples = tags.get(_TIFF_SAMPLES_PER_PIXEL, 1)
                per = max(4, (int(samples) * int(bits) + 7) // 8)  # libtiff's RGBA path uses 4
                tile_w, tile_h = tags.get(_TIFF_TILE_WIDTH), tags.get(_TIFF_TILE_LENGTH)
                if tile_w and tile_h:
                    block = int(tile_w) * int(tile_h)
                else:
                    stored_w = int(tags.get(_TIFF_IMAGE_WIDTH) or w)
                    stored_h = int(tags.get(_TIFF_IMAGE_LENGTH) or h)
                    rows = int(tags.get(_TIFF_ROWS_PER_STRIP) or stored_h)
                    block = stored_w * max(1, min(stored_h, rows))
                extra += block * per
            except (TypeError, ValueError):  # odd tag types: assume one whole-image strip
                extra += w * h * 8
        if tags.get(_EXIF_ORIENTATION, 1) in _ORIENTATION:
            extra += w * h * _mode_bytes(im.mode)
        return extra
    if im.format == "BMP" and im.info.get("compression") in _BMP_RLE:
        return 2 * w * h
    return 0


def _draft_size(size: tuple[int, int], long_edge: int) -> tuple[int, int] | None:
    """The size to ask ``JpegImageFile.draft`` for: the master's size, so
    libjpeg picks the largest DCT scale (1/2, 1/4, 1/8) that still decodes at
    least that many pixels in both directions.  None when no reduction is
    possible.

    Not twice the master, as ``Image.thumbnail``'s ``reducing_gap`` would ask:
    libjpeg's scaled IDCT is already a proper area filter, and the master is
    reduced another 3x when it is fitted to the panel, so the extra pixels buy
    nothing visible.  They cost a lot: a 24 MP phone JPEG (5712 px) would not
    be drafted at all and decode at 98 MB instead of 24 MB.
    """
    w, h = size
    k = long_edge / max(w, h)
    if k >= 0.5:  # draft only reduces by 2, 4 or 8
        return None
    return (max(1, math.ceil(w * k)), max(1, math.ceil(h * k)))


def _orientation(im: Image.Image) -> int:
    """The EXIF/XMP orientation Pillow reports (1 when absent or unreadable).

    Only called on a loaded image: ``PngImageFile.getexif()`` decodes the
    whole file when no eXIf chunk precedes the pixels, and Pillow's TIFF
    plugin applies the orientation while loading and then drops the tag, so
    a loaded TIFF reports 1 and is not turned a second time."""
    try:
        value = im.getexif().get(_EXIF_ORIENTATION, 1)
    except Exception:  # a malformed EXIF block must not refuse the photo
        log.debug("unreadable EXIF; assuming orientation 1", exc_info=True)
        return 1
    return value if isinstance(value, int) else 1


def _float_scale(im: Image.Image) -> float:
    """The factor a float (``F``) image's samples are multiplied by before
    they are clipped to 0..255: 255 when every sample lies in 0..1 (how
    float images are usually normalised), else 1 (taken as 0..255).  Read
    once from the whole image (``getextrema``, no copy), so every strip of
    ``normalise`` is scaled alike."""
    if im.mode != "F":
        return 1.0
    hi = im.getextrema()[1]
    return 255.0 if isinstance(hi, float) and math.isfinite(hi) and hi <= 1.0 else 1.0


def _to_8bit(im: Image.Image, float_scale: float | None = None) -> Image.Image:
    """Bring bilevel, palette, 16/32-bit and float modes to an 8-bit mode the
    rest of the pipeline handles (L, LA, RGB, RGBA or CMYK).  A ``tRNS``
    colour key (``info["transparency"]`` on an L, RGB, bilevel or 16-bit
    image) becomes an alpha channel, so it is composited like any other.
    Float samples are multiplied by ``float_scale`` (default: ``im``'s own
    ``_float_scale``) and clipped."""
    mode = im.mode
    key = im.info.get("transparency")
    if mode == "1":
        return im.convert("L" if key is None else "LA")
    if mode in ("P", "PA"):
        return im.convert("RGBA" if mode == "PA" or key is not None else "RGB")
    if mode.startswith("I;16") or mode == "I":
        # 16-bit samples: 0..65535 -> 0..255 (convert("L") would clip, not scale)
        wide = im if mode == "I" else im.convert("I")
        grey = wide.point(lambda v: v * (1 / 257)).convert("L")
        if not isinstance(key, int):
            return grey
        # the key is a 16-bit sample (Pillow's own conversion compares clipped values)
        alpha = np.where(np.asarray(im) == key, 0, 255).astype(np.uint8)
        return Image.merge("LA", (grey, Image.fromarray(alpha, "L")))
    if mode == "F":
        scale = _float_scale(im) if float_scale is None else float_scale
        return (im if scale == 1 else im.point(lambda v: v * scale)).convert("L")
    if mode in ("La", "RGBa"):
        return im.convert(mode[0] + "A" if mode == "La" else "RGBA")
    if mode in ("L", "RGB") and key is not None:
        return im.convert(mode + "A" if mode == "L" else "RGBA")
    if mode in ("L", "LA", "RGB", "RGBA", "CMYK"):
        return im
    return im.convert("RGB")  # YCbCr, LAB, HSV, RGBX, ...


def _flatten_alpha(im: Image.Image) -> Image.Image:
    """Composite RGBA on white as RGB and LA on white as L; other modes pass
    through."""
    if im.mode == "RGBA":
        out = Image.new("RGB", im.size, WHITE_RGB)
        out.paste(im, mask=im.getchannel("A"))
        return out
    if im.mode == "LA":
        out = Image.new("L", im.size, 255)
        out.paste(im.getchannel("L"), mask=im.getchannel("A"))
        return out
    return im


def normalise(im: Image.Image, factor: int, *, copy: bool = False) -> Image.Image:
    """A loaded image as 8-bit L, RGB or CMYK with any transparency
    composited on white, box-reduced ``factor`` times (``Image.reduce``).

    Converting the whole image at once would hold another full-size copy (a
    24 MP palette PNG expanded to RGBA is 96 MB, and ``reduce`` premultiplies
    RGBA into 96 MB more), so a mode that needs converting is done in
    horizontal strips of whole multiples of ``factor`` rows: each is
    converted, composited and reduced before the next.  The result is the
    whole-image one, since compositing on white before a box average is the
    same as averaging premultiplied alpha and compositing after.  An image
    that needs no conversion is reduced in one call (which only allocates
    the result), or returned as it is (``copy``: as a copy, so the caller can
    drop the decoder that is attached to the original).
    """
    if not _needs_conversion(im.mode, "transparency" in im.info):
        if factor >= 2:
            return im.reduce(factor)
        return im.copy() if copy else im
    w, h = im.size
    rows = factor * max(1, _STRIP_PIXELS // (w * factor))
    scale = _float_scale(im)
    out: Image.Image | None = None
    for y0 in range(0, h, rows):
        strip = _flatten_alpha(_to_8bit(im.crop((0, y0, w, min(h, y0 + rows))), scale))
        if factor >= 2:
            strip = strip.reduce(factor)
        if out is None:
            out = Image.new(strip.mode, (-(-w // factor), -(-h // factor)))
        out.paste(strip, (0, y0 // factor))
    if out is None:  # unreachable: _check_pixels refuses empty images
        raise UndecodableImage(f"image has no pixels ({w} x {h})")
    return out


_SRGB = None


def _to_srgb(im: Image.Image, icc: bytes | None) -> Image.Image:
    """``im`` as sRGB RGB: through its embedded ICC profile when littlecms can
    use it (iPhone photos are Display P3), else a plain ``convert("RGB")``."""
    global _SRGB
    if icc and im.mode in ("RGB", "CMYK", "L"):
        try:
            if _SRGB is None:
                _SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            out = ImageCms.profileToProfile(
                im, src, _SRGB, renderingIntent=ImageCms.Intent.PERCEPTUAL, outputMode="RGB"
            )
            if out is not None:
                return out
        except Exception:  # unusable or mismatched profile: keep the numbers as they are
            log.info("ignoring an embedded ICC profile littlecms cannot use", exc_info=True)
    return im if im.mode == "RGB" else im.convert("RGB")


def _scaled_size(size: tuple[int, int], long_edge: int) -> tuple[int, int]:
    w, h = size
    s = long_edge / max(w, h)
    return (max(1, round(w * s)), max(1, round(h * s)))


def _check_pixels(w: int, h: int, max_pixels: int) -> None:
    if w < 1 or h < 1:
        raise UndecodableImage(f"image has no pixels ({w} x {h})")
    if w * h > max_pixels:
        raise UndecodableImage(
            f"image is {w} x {h} ({w * h / 1e6:.1f} MP); the limit is {max_pixels / 1e6:g} MP"
        )


def _check_decoder_time(im: Image.Image) -> None:
    """Refuse an image whose decoder runs in Python and would hold the render
    thread too long: an RLE BMP over ``MAX_RLE_BMP_PIXELS`` (its end-of-line
    padding alone costs a loop step per pixel, however small the file)."""
    w, h = im.size
    if im.format == "BMP" and im.info.get("compression") in _BMP_RLE and w * h > MAX_RLE_BMP_PIXELS:
        raise UndecodableImage(
            f"image is a {w} x {h} ({w * h / 1e6:.1f} MP) RLE-compressed BMP; this frame decodes"
            f" those up to {MAX_RLE_BMP_PIXELS / 1e6:g} MP. Upload a PNG or JPEG version"
        )


def _check_cost(w: int, h: int, mode: str, cost: int, max_decode_bytes: int) -> None:
    if cost > max_decode_bytes:
        raise UndecodableImage(
            f"image is {w} x {h} ({w * h / 1e6:.1f} MP {mode}); decoding it needs about "
            f"{cost / 1e6:.0f} MB, more than this frame allows ({max_decode_bytes / 1e6:.0f}"
            " MB). Upload a smaller or JPEG version"
        )


def reduce_rows(
    buf: bytes | memoryview,
    mode: str,
    size: tuple[int, int],
    stride: int,
    factor: int,
    raw_mode: str | None = None,
) -> Image.Image:
    """``Image.frombytes(mode, size, buf, "raw", raw_mode, stride).reduce(factor)``
    without ever holding the full-size Pillow image: the raw rows are converted
    and reduced in horizontal strips (whole multiples of ``factor`` rows, so the
    result is identical).  ``factor < 2`` is a plain copy."""
    w, h = size
    raw = raw_mode or mode
    if factor < 2:
        return Image.frombytes(mode, size, buf, "raw", raw, stride)
    view = memoryview(buf)
    rows = factor * max(1, _STRIP_BYTES // (stride * factor))
    out = Image.new(mode, (-(-w // factor), -(-h // factor)))
    for y0 in range(0, h, rows):
        y1 = min(h, y0 + rows)
        strip = Image.frombytes(
            mode, (w, y1 - y0), view[y0 * stride : y1 * stride], "raw", raw, stride
        )
        out.paste(strip.reduce(factor), (0, y0 // factor))
    return out


def _decode_heif(
    data: bytes, *, max_pixels: int, max_decode_bytes: int, long_edge: int
) -> tuple[Image.Image, int, bytes | None]:
    """HEIC/HEIF through pillow-heif's own API rather than its Pillow plugin:
    libheif decodes into its buffer and ``reduce_rows`` hands Pillow
    box-reduced strips, so Pillow never holds a full-size (4 bytes per pixel)
    copy: a 24 MP iPhone photo peaks 119 MB above the baseline instead of
    181 MB through the plugin.  libheif applies the file's rotation and mirror
    boxes itself, and the HEIF spec says the EXIF orientation must then be
    ignored, so the orientation returned is always 1."""
    import pillow_heif

    try:
        heif = pillow_heif.open_heif(io.BytesIO(data), convert_hdr_to_8bit=True)
        frame = heif[heif.primary_index]
        w, h = frame.size
        mode = frame.mode
    except Exception as exc:
        raise UndecodableImage(f"cannot decode this HEIF file: {exc}") from None
    _check_pixels(w, h, max_pixels)
    if mode not in ("RGB", "RGBA", "L", "LA"):
        raise UndecodableImage(f"unsupported HEIF pixel format {mode!r}")
    cost = decode_cost(mode, (w, h), "heif", long_edge) + len(data)
    _check_cost(w, h, mode, cost, max_decode_bytes)
    icc = frame.info.get("icc_profile") or None
    try:
        buf = frame.data  # decodes; the size can change here (clean-aperture crops)
        w, h = frame.size
        raw = "RGBa" if mode == "RGBA" and frame.premultiplied_alpha else mode
        img = reduce_rows(buf, mode, (w, h), frame.stride, _reduce_factor((w, h), long_edge), raw)
    except IngestError:
        raise
    except MemoryError:
        raise UndecodableImage("not enough memory to decode this image") from None
    except Exception as exc:
        raise UndecodableImage(f"cannot decode this HEIF file: {exc}") from None
    return img, 1, icc


def _decode_pillow(
    data: bytes, source_format: str, *, max_pixels: int, max_decode_bytes: int, long_edge: int
) -> tuple[Image.Image, int, bytes | None]:
    """Every format but HEIF: Pillow with the one plugin the sniffer chose.
    Returns the decoded image (``normalise``d: 8-bit, on white, reduced;
    not yet oriented), its EXIF orientation and its ICC profile.

    Nothing is decoded before the budget check: the estimate uses only the
    raw bytes (``scan_metadata``, before Pillow parses them) and what
    ``Image.open`` parsed from the headers, and the orientation is read
    after ``load()`` (see ``_orientation``).

    ``Image.MAX_IMAGE_PIXELS`` is left alone (it is process-wide, and any
    other thread opening an image would see a changed value): ``Image.open``
    only parses headers, and ``_check_pixels`` refuses anything over
    ``max_pixels`` straight after it.  Pillow's own limit (twice its
    default, about 179 MP) still refuses a bigger image inside the open."""
    plugin = FORMATS[source_format]
    meta = scan_metadata(data, source_format)
    # ``Image.open`` already decodes a PNG's text before its pixels, so the
    # metadata alone must fit before Pillow parses anything
    head = round((meta.retained + meta.transient) * _COST_MARGIN) + len(data)
    if head > max_decode_bytes:
        raise UndecodableImage(
            f"the file's metadata (text, comments, profiles, EXIF tags) needs about"
            f" {head / 1e6:.0f} MB to read, more than this frame allows"
            f" ({max_decode_bytes / 1e6:.0f} MB). It is too large to decode safely;"
            " re-save it without the metadata"
        )
    try:
        # BytesIO shares ``data`` (bytes) rather than copying it, and so does
        # its getvalue(), which libtiff's in-memory path decodes from
        im = Image.open(io.BytesIO(data), formats=[plugin])
    except Image.DecompressionBombError as exc:
        raise UndecodableImage(f"image refused as a decompression bomb: {exc}") from None
    except Exception as exc:  # UnidentifiedImageError (an OSError), SyntaxError, ...
        raise UndecodableImage(f"cannot decode this {plugin} file: {exc}") from None

    w, h = im.size
    _check_pixels(w, h, max_pixels)
    _check_decoder_time(im)
    icc = im.info.get("icc_profile") or None
    try:
        extra = decoder_overhead(im, data)  # before the draft: it needs the full size
        if plugin == "JPEG":
            req = _draft_size(im.size, long_edge)
            if req is not None:
                im.draft("RGB", req)
        cost = decode_cost(
            im.mode,
            im.size,
            source_format,
            long_edge,
            decoder_bytes=extra + meta.transient,
            retained_bytes=meta.retained,
            transparency="transparency" in im.info,
        )
        _check_cost(w, h, im.mode, cost + len(data), max_decode_bytes)
        im.load()
        orientation = _orientation(im)
        img = normalise(im, _reduce_factor(im.size, long_edge), copy=plugin == "WEBP")
    except IngestError:
        raise
    except MemoryError:
        raise UndecodableImage("not enough memory to decode this image") from None
    except Exception as exc:  # truncated data, broken streams, codec errors
        raise UndecodableImage(f"cannot decode this {plugin} file: {exc}") from None
    if img is not im:
        im.close()  # free the full-size decode now (its decoder goes with ``im``)
    return img, orientation, icc


def decode_master(
    data: bytes,
    source_format: str,
    *,
    max_pixels: int = MAX_PIXELS,
    max_decode_bytes: int = MAX_DECODE_BYTES,
    long_edge: int = MASTER_LONG_EDGE,
) -> Image.Image:
    """Decode ``data`` (already sniffed as ``source_format``) into an upright
    sRGB ``RGB`` image whose long edge is at most ``long_edge``.

    Raises ``UndecodableImage`` (bombs, oversize, corrupt or truncated data)
    or ``UnsupportedImageType`` (HEIC without a usable pillow-heif).
    """
    limits = {
        "max_pixels": max_pixels,
        "max_decode_bytes": max_decode_bytes,
        "long_edge": long_edge,
    }
    if source_format == "heif":
        if not heif_available():
            raise UnsupportedImageType("HEIC support (pillow-heif) is not available on this frame")
        img, orientation, icc = _decode_heif(data, **limits)
        img = _flatten_alpha(img)
    else:
        img, orientation, icc = _decode_pillow(data, source_format, **limits)

    # resize first, then orient: the transpose then copies a master-size
    # image, not the decoded one (the long edge is the same either way)
    if max(img.size) > long_edge:
        img = img.resize(_scaled_size(img.size, long_edge), Image.Resampling.LANCZOS)
    method = _ORIENTATION.get(orientation)
    if method is not None:
        img = img.transpose(method)
    img = _to_srgb(img, icc)
    img.info = {}  # never carry EXIF, ICC or XMP into the master
    return img


def encode_master(img: Image.Image) -> bytes:
    """The master JPEG: baseline, q92, optimised Huffman tables, no metadata."""
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=MASTER_QUALITY, optimize=True, progressive=False)
    return buf.getvalue()


def ingest(
    data: bytes,
    original_name: str | None,
    *,
    paths: DataPaths,
    photo_id: str | None = None,
    max_bytes: int = MAX_UPLOAD_BYTES,
    max_pixels: int = MAX_PIXELS,
    max_decode_bytes: int = MAX_DECODE_BYTES,
) -> Master:
    """Validate an upload, store its master and return what was stored.

    Raises ``UploadTooLarge`` (413), ``UnsupportedImageType`` (415) or
    ``UndecodableImage`` (422); nothing is written in those cases.  The
    caller inserts the database row (and deletes the master if that fails).

    ``photo_id`` must be a fresh photo id (``ValueError`` otherwise, before
    anything is decoded): masters are never replaced, which is what lets a
    render key leave the master out (see ``pipeline``).
    """
    if photo_id is not None:
        _require_id(photo_id)
    size = len(data)
    if size > max_bytes:
        raise UploadTooLarge(
            f"the file is {size / 1e6:.1f} MB; the limit is {max_bytes / (1024 * 1024):g} MB"
        )
    fmt = sniff_format(data)
    img = decode_master(data, fmt, max_pixels=max_pixels, max_decode_bytes=max_decode_bytes)
    jpeg = encode_master(img)
    pid = photo_id or new_id()
    path = master_path(paths, pid)
    if path.exists():
        raise FileExistsError(f"photo {pid} already has a master; masters are never replaced")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, jpeg)
    w, h = img.size
    master = Master(
        id=pid,
        path=path,
        width=w,
        height=h,
        portrait=h > w,
        bytes=len(jpeg),
        original_name=sanitize_name(original_name),
        source_format=fmt,
    )
    log.info(
        "ingested %s (%s, %.1f MB) as %s: %dx%d, %d bytes",
        master.original_name,
        fmt,
        size / 1e6,
        pid,
        w,
        h,
        master.bytes,
    )
    return master


def delete_master(paths: DataPaths, photo_id: str) -> bool:
    """Remove a photo's master file; False if it was already gone.
    ``ValueError`` for a malformed id."""
    try:
        master_path(paths, photo_id).unlink()
    except FileNotFoundError:
        return False
    return True
