"""Print the dither-engine figures quoted in docs/DITHER.md, so they can be re-checked.

Run from the repo root with the native library built (desktop venv shown):

    .venv/Scripts/python scripts/dither_stats.py               # spreads, flat areas, greys
    .venv/Scripts/python scripts/dither_stats.py --lab-cube    # + all 2^24 colours (~15 s)

Sections:

* **spreads**: the ordered methods' palette-tied spread (8-bit levels) for the
  nominal and measured palettes, rgb and lab.
* **flat**: every method, strength 0.5 / 0.85 / 1.0 and metric on 64 x 64 flat
  areas of each palette colour, pure black and pure white; prints the number
  of pixels that are not the area's nearest colour (all zero by design).
* **greys**: for flat greys and the measured palette, which greys ``none``
  maps entirely to green, and the colour shares of a few greys per method.
* **lab-cube** (opt-in): the engine's integer L*a*b* against a float64
  conversion over every sRGB colour (max and mean dE), and how often the
  engine's nearest colour differs from float CIE76, with the worst margin.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inkframe.display.palette import MEASURED_DEFAULT, NAMES, NOMINAL  # noqa: E402
from inkframe.render import dither as dm  # noqa: E402
from tests import dither_reference as ref  # noqa: E402

PALETTES = {"nominal": NOMINAL, "measured": MEASURED_DEFAULT}


def spreads() -> None:
    print("ordered spread at strength 1.0 (8-bit levels; cap 96)")
    for name, pal in PALETTES.items():
        cells = "  ".join(f"{d} {dm.ordered_spread(pal, d):7.4f}" for d in dm.DISTANCES)
        print(f"  {name:<9} {cells}")


def flat() -> None:
    print("flat palette colours, pure black and pure white: pixels off their nearest colour")
    img = np.empty((64, 64, 3), dtype=np.uint8)
    total = 0
    for name, pal in PALETTES.items():
        for d in dm.DISTANCES:
            off = 0
            for c in (*pal.rgb, (0, 0, 0), (255, 255, 255)):
                img[:] = c
                want = dm.dither(img[:1, :1], pal, "none", distance=d)[0, 0]
                for m in dm.METHOD_IDS:
                    for s in (0.5, 0.85, 1.0):
                        off += int(
                            np.count_nonzero(dm.dither(img, pal, m, strength=s, distance=d) != want)
                        )
            total += off
            print(f"  {name:<9} {d}: {off}")
    print(f"  total {total}")


def _shares(grey: int, method: str, distance: str) -> np.ndarray:
    img = np.full((128, 128, 3), grey, dtype=np.uint8)
    out = dm.dither(img, MEASURED_DEFAULT, method, distance=distance)
    return np.bincount(out.ravel(), minlength=6) / out.size


def greys() -> None:
    print("flat greys, measured palette, default strength 0.85 (colour shares)")
    for d in dm.DISTANCES:
        green = [g for g in range(256) if _shares(g, "none", d)[5] == 1.0]
        span = f"{green[0]}..{green[-1]}" if green else "none"
        print(f"  none/{d}: greys mapped 100 % to green: {span}")
    for m, d in (("atkinson", "rgb"), ("atkinson", "lab"), ("floyd_steinberg", "lab"),
                 ("bayer_8", "rgb"), ("bayer_8", "lab"), ("blue_noise", "lab")):  # fmt: skip
        print(f"  {m}/{d}:")
        for g in (64, 96, 128, 160, 192):
            sh = _shares(g, m, d)
            parts = ", ".join(f"{NAMES[i]} {sh[i] * 100:.0f} %" for i in range(6) if sh[i] >= 0.005)
            print(f"    grey {g:3d}: {parts}")
        if (m, d) == ("atkinson", "lab"):
            g = [_shares(v, m, d)[5] * 100 for v in range(64, 161)]
            print(f"    green share over greys 64..160: {min(g):.1f} % .. {max(g):.1f} %")


def lab_cube() -> None:
    print("engine Lab vs float64 over all 2^24 sRGB colours")
    worst, at, total = 0.0, None, 0.0
    pal_lab = {k: ref.srgb_to_lab(p.as_array()) for k, p in PALETTES.items()}
    differ = dict.fromkeys(PALETTES, 0)
    margin = dict.fromkeys(PALETTES, 0.0)
    g, b = np.meshgrid(np.arange(256), np.arange(256), indexing="ij")
    for r in range(256):
        cols = np.stack([np.full_like(g, r), g, b], axis=-1).reshape(-1, 3)
        exact = ref.srgb_to_lab(cols)
        de = np.linalg.norm(dm.engine_lab(cols) - exact, axis=1)
        i = int(de.argmax())
        if de[i] > worst:
            worst, at = float(de[i]), tuple(int(v) for v in cols[i])
        total += float(de.sum())
        img = cols.astype(np.uint8).reshape(256, 256, 3)
        for k, p in PALETTES.items():
            got = dm.dither(img, p, "none", distance="lab").ravel()
            dist = np.linalg.norm(exact[:, None, :] - pal_lab[k][None], axis=2)
            want = dist.argmin(axis=1)
            bad = np.nonzero(got != want)[0]
            if bad.size:
                differ[k] += int(bad.size)
                extra = dist[bad, got[bad]] - dist[bad, want[bad]]
                margin[k] = max(margin[k], float(extra.max()))
    print(f"  max dE {worst:.4f} at {at}, mean {total / 2**24:.4f}")
    for k in PALETTES:
        print(
            f"  nearest colour differs from float CIE76 for {differ[k]} colours ({k}); "
            f"the engine's pick is at most {margin[k]:.3f} dE farther"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lab-cube", action="store_true", help="also scan all 2^24 colours")
    args = ap.parse_args(argv)
    spreads()
    flat()
    greys()
    if args.lab_cube:
        lab_cube()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
