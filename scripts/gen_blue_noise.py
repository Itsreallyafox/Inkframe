"""Generate inkframe/render/blue_noise_64.npy: the 64x64 blue-noise threshold
matrix used by the ``blue_noise`` dither method.

How the committed file was made (run from the repo root, desktop venv):

    .venv/Scripts/python scripts/gen_blue_noise.py

Algorithm: Ulichney's void-and-cluster ("The void-and-cluster method for dither
array generation", SPIE 1913, 1993) on a 64x64 torus:

1. Seed a random binary pattern with 10% ones (``numpy.random.default_rng(SEED)``).
2. Relax it: repeatedly move the tightest cluster (the one with the highest
   Gaussian-filtered energy) into the largest void (lowest energy) until the
   two coincide.  This is the initial binary pattern.
3. Phase 1: remove tightest clusters one by one from a copy, ranking them
   ``ones - 1`` down to 0.
4. Phases 2 and 3: insert into the largest void one by one, ranking
   ``ones`` up to 4095.  (Ulichney's phase 3 swaps the roles of 0s and 1s;
   with a normalised toroidal filter "tightest cluster of 0s" is exactly
   "largest void of 1s", so one loop covers both.)

Energy is the binary pattern convolved (toroidally, via FFT once, then updated
incrementally) with a Gaussian of sigma ``SIGMA``.  Ties break to the lowest
flat index (``numpy.argmin``/``argmax``), so the output depends only on
``SEED``, ``SIGMA`` and numpy's PCG64 stream.

Output: ``uint8`` values ``rank * 256 // 4096`` (0..255, each value exactly 16
times), row-major, saved with ``numpy.save`` (no pickle).  The script then
checks the result is actually blue: the share of spectral power at low spatial
frequencies is compared with white noise (random permutations of the same
values) and printed; it must be well below 1.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

SIZE = 64
SEED = 20261019
SIGMA = 1.9
INITIAL_FRACTION = 0.10
#: radial frequency (cycles/pixel) below which power counts as "low"
LOW_FREQ = 0.125

OUT = Path(__file__).resolve().parent.parent / "inkframe" / "render" / "blue_noise_64.npy"


def gaussian_kernel(n: int, sigma: float) -> np.ndarray:
    """Toroidal Gaussian centred on (0, 0) of an n x n torus."""
    d = np.minimum(np.arange(n), n - np.arange(n)).astype(np.float64)
    g = np.exp(-(d**2) / (2.0 * sigma**2))
    return np.outer(g, g)


def void_and_cluster(n: int = SIZE, sigma: float = SIGMA, seed: int = SEED) -> np.ndarray:
    """Return the ``(n, n)`` int rank matrix 0..n*n-1."""
    total = n * n
    kernel = gaussian_kernel(n, sigma)
    kf = np.fft.rfft2(kernel)

    def kern(i: int) -> np.ndarray:
        """The kernel centred on flat index ``i`` (not cached: 4096 copies would be 128 MB)."""
        y, x = divmod(i, n)
        return np.roll(kernel, (y, x), axis=(0, 1)).ravel()

    def energy(pattern: np.ndarray) -> np.ndarray:
        img = pattern.reshape(n, n).astype(np.float64)
        return np.fft.irfft2(np.fft.rfft2(img) * kf, s=(n, n)).ravel()

    def tightest_cluster(pattern: np.ndarray, e: np.ndarray) -> int:
        return int(np.argmax(np.where(pattern, e, -np.inf)))

    def largest_void(pattern: np.ndarray, e: np.ndarray) -> int:
        return int(np.argmin(np.where(pattern, np.inf, e)))

    rng = np.random.default_rng(seed)
    ones = int(round(total * INITIAL_FRACTION))
    pattern = np.zeros(total, dtype=bool)
    pattern[rng.choice(total, size=ones, replace=False)] = True

    # 2. relax into the initial binary pattern
    e = energy(pattern)
    for _ in range(100 * total):
        c = tightest_cluster(pattern, e)
        pattern[c] = False
        e -= kern(c)
        v = largest_void(pattern, e)
        if v == c:
            pattern[c] = True
            e += kern(c)
            break
        pattern[v] = True
        e += kern(v)
    else:  # pragma: no cover - never seen; would mean the relaxation cycles
        raise RuntimeError("initial pattern did not converge")

    rank = np.full(total, -1, dtype=np.int64)

    # 3. phase 1: peel clusters off a copy
    p1, e1 = pattern.copy(), e.copy()
    for r in range(ones - 1, -1, -1):
        c = tightest_cluster(p1, e1)
        p1[c] = False
        e1 -= kern(c)
        rank[c] = r

    # 4. phases 2 + 3: fill voids
    p2, e2 = pattern.copy(), e.copy()
    for r in range(ones, total):
        v = largest_void(p2, e2)
        p2[v] = True
        e2 += kern(v)
        rank[v] = r

    assert (rank >= 0).all() and np.array_equal(np.sort(rank), np.arange(total))
    return rank.reshape(n, n)


def low_frequency_share(tex: np.ndarray, cutoff: float = LOW_FREQ) -> float:
    """Share of the (DC-free) power spectrum at radial frequency < ``cutoff``."""
    t = tex.astype(np.float64)
    p = np.abs(np.fft.fft2(t - t.mean())) ** 2
    fy = np.fft.fftfreq(t.shape[0])[:, None]
    fx = np.fft.fftfreq(t.shape[1])[None, :]
    r = np.hypot(fy, fx)
    nonzero = r > 0
    return float(p[(r < cutoff) & nonzero].sum() / p[nonzero].sum())


def blueness(tex: np.ndarray, trials: int = 32, cutoff: float = LOW_FREQ) -> float:
    """Low-frequency share of ``tex`` divided by that of white noise with the
    same values (mean over ``trials`` seeded permutations).  Blue noise << 1."""
    rng = np.random.default_rng(1)
    white = np.mean(
        [
            low_frequency_share(rng.permutation(tex.ravel()).reshape(tex.shape), cutoff)
            for _ in range(trials)
        ]
    )
    return low_frequency_share(tex, cutoff) / white


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT, help=f"output .npy (default {OUT})")
    args = ap.parse_args(argv)

    rank = void_and_cluster()
    tex = (rank * 256 // (SIZE * SIZE)).astype(np.uint8)
    counts = np.bincount(tex.ravel(), minlength=256)
    assert counts.min() == counts.max() == SIZE * SIZE // 256, counts

    for cutoff in (0.0625, 0.125, 0.25):
        print(
            f"low-frequency power (f < {cutoff:g} cyc/px) vs white noise: "
            f"{blueness(tex, cutoff=cutoff):.4f}"
        )
    ratio = blueness(tex)
    if ratio > 0.25:
        print(f"texture is not blue enough (ratio {ratio:.3f})", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.save(args.out, tex, allow_pickle=False)
    print(f"wrote {args.out} ({tex.shape[0]}x{tex.shape[1]} uint8, seed {SEED}, sigma {SIGMA})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
