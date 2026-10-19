"""Generate tests/golden/dither_golden.json: SHA-256 of the engine's output for a
fixed input across every method and knob combination.

Run from the repo root (only when the engine's output is meant to change, and
review the diff of the JSON like code):

    .venv/Scripts/python -m tests.gen_dither_golden          # desktop
    /opt/inkframe/.venv/bin/python -m tests.gen_dither_golden  # Pi

``test_dither.py::test_golden_hashes`` compares every entry against the
engine; because the engine is integer-only the same file must pass on the
desktop (MSVC/x64) and on the Pi (gcc/aarch64).

The input is generated in code, not stored: ``make_golden_image()`` builds a
160x100 RGB image from integer arithmetic and numpy's PCG64 stream only (no
floating-point maths whose last bit could differ between platforms): a grey
ramp, a hue sweep fading to white and black, flat patches of every nominal and
measured palette colour plus skin and sky tones, and seeded noise.  Its
SHA-256 is stored in the JSON so a changed generator is reported as such.

JSON layout::

    {
      "format": 1,
      "image": {"generator": ..., "width": 160, "height": 100, "seed": ..., "sha256": ...},
      "strength": 0.85,
      "palettes": {"nominal": [[r, g, b] x 6], "measured": [...]},
      "hash": "sha256 of out_index.tobytes() ((H, W) uint8, row-major)",
      "hashes": {
        "<method>|<on|off>|<rgb|lab>|<nominal|measured>": "<sha256>",          # strength 0.85
        "<method>|<on|off>|<rgb|lab>|<nominal|measured>|<strength>": "<sha256>" # extra rows
      }
    }

The main grid is all 12 methods x serpentine on/off x rgb/lab x nominal/measured
at strength 0.85; the extra rows are every method at strength 1.0 and 0.5
(serpentine on, lab, measured), which covers each kind.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from inkframe.display.palette import MEASURED_DEFAULT, NOMINAL, Palette
from inkframe.render.dither import METHODS, dither

GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "dither_golden.json"
GOLDEN_FORMAT = 1
GOLDEN_SEED = 20261019
WIDTH, HEIGHT = 160, 100
STRENGTH = 0.85
EXTRA_STRENGTHS = (1.0, 0.5)
PALETTES: dict[str, Palette] = {"nominal": NOMINAL, "measured": MEASURED_DEFAULT}


def _hue(x: np.ndarray, width: int) -> np.ndarray:
    """Integer HSV hue sweep at full saturation/value: (len(x), 3) int64."""
    h = x * 1530 // width  # 6 segments of 255
    seg, f = h // 255, h % 255
    z, full = np.zeros_like(f), np.full_like(f, 255)
    table = [
        (full, f, z),
        (255 - f, full, z),
        (z, full, f),
        (z, 255 - f, full),
        (f, z, full),
        (full, z, 255 - f),
    ]
    out = np.zeros((x.size, 3), dtype=np.int64)
    for s, (r, g, b) in enumerate(table):
        m = seg == s
        out[m] = np.stack([r[m], g[m], b[m]], axis=1)
    return out


def make_golden_image() -> np.ndarray:
    """The deterministic 160x100 golden input (uint8, (100, 160, 3))."""
    rng = np.random.default_rng(GOLDEN_SEED)
    img = np.zeros((HEIGHT, WIDTH, 3), dtype=np.int64)
    x = np.arange(WIDTH, dtype=np.int64)

    # rows 0-15: grey ramp 0..255
    img[0:16] = (x * 255 // (WIDTH - 1))[None, :, None]

    # rows 16-55: hue sweep, fading to white (top) and to black (bottom)
    hue = _hue(x, WIDTH)  # (W, 3)
    for r in range(40):
        y = 16 + r
        if r < 20:
            k = 80 + r * 175 // 19  # 80..255 of the hue, rest white
            img[y] = (hue * k + 255 * (255 - k)) // 255
        else:
            k = 255 - (r - 20) * 200 // 19  # 255..55 of the hue, rest black
            img[y] = hue * k // 255

    # rows 56-75: flat patches, 10 px wide: nominal x6, measured x6, skin, sky, greys
    patches = [*NOMINAL.rgb, *MEASURED_DEFAULT.rgb]
    patches += [(224, 172, 140), (141, 85, 36), (120, 170, 230), (200, 225, 245)]
    for i, c in enumerate(patches):
        img[56:76, i * 10 : (i + 1) * 10] = c

    # rows 76-87: two-colour vertical gradient (sky to sand)
    t = np.arange(12, dtype=np.int64)[:, None, None]
    top, bottom = np.array([70, 130, 200]), np.array([230, 200, 150])
    img[76:88] = (top * (11 - t) + bottom * t) // 11

    # rows 88-99: uniform noise (exercises clamping and every palette entry)
    img[88:100] = rng.integers(0, 256, size=(12, WIDTH, 3))

    # light noise over everything above, so no two rows are identical
    img[0:88] += rng.integers(-6, 7, size=(88, WIDTH, 3))
    return np.clip(img, 0, 255).astype(np.uint8)


def sha256(a: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


def golden_cases() -> list[tuple[str, str, bool, str, str, float]]:
    """(key, method, serpentine, distance, palette, strength) for every row."""
    cases = []
    for m in METHODS:
        for serp in (True, False):
            for dist in ("rgb", "lab"):
                for pal in PALETTES:
                    key = f"{m.id}|{'on' if serp else 'off'}|{dist}|{pal}"
                    cases.append((key, m.id, serp, dist, pal, STRENGTH))
    for m in METHODS:
        for s in EXTRA_STRENGTHS:
            cases.append((f"{m.id}|on|lab|measured|{s}", m.id, True, "lab", "measured", s))
    return cases


def parse_key(key: str, default_strength: float) -> tuple[str, bool, str, str, float]:
    """``"<method>|<on|off>|<dist>|<pal>[|<strength>]"`` -> its parts."""
    parts = key.split("|")
    if len(parts) not in (4, 5) or parts[1] not in ("on", "off"):
        raise ValueError(f"bad golden key {key!r}")
    s = float(parts[4]) if len(parts) == 5 else default_strength
    return parts[0], parts[1] == "on", parts[2], parts[3], s


def build() -> dict:
    img = make_golden_image()
    hashes = {}
    for key, method, serp, dist, pal, s in golden_cases():
        out = dither(img, PALETTES[pal], method, serpentine=serp, strength=s, distance=dist)
        hashes[key] = sha256(out)
    return {
        "format": GOLDEN_FORMAT,
        "image": {
            "generator": "tests.gen_dither_golden.make_golden_image",
            "width": WIDTH,
            "height": HEIGHT,
            "seed": GOLDEN_SEED,
            "sha256": sha256(img),
        },
        "strength": STRENGTH,
        "palettes": {name: [list(c) for c in p.rgb] for name, p in PALETTES.items()},
        "hash": "sha256 of out_index.tobytes() ((H, W) uint8, row-major)",
        "hashes": hashes,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write the dither golden-hash file.")
    ap.add_argument("--out", type=Path, default=GOLDEN_PATH, help=f"default {GOLDEN_PATH}")
    args = ap.parse_args(argv)
    data = build()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {len(data['hashes'])} hashes to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
