# Dither engine: design notes

`inkframe/render/dither.c` turns an RGB image into palette indices. It is
loaded with ctypes by `inkframe/render/dither.py`, which holds the method
registry the UI shows. There is no Python fallback: if the library has not been
built, `dither()` raises `DitherLibraryMissing` with the build command.

| Platform | Library | Built by |
|---|---|---|
| Pi (gcc 14, aarch64) | `inkframe/render/libdither.so` | `install.sh` `build_native` on every deploy that touches `inkframe/render/*.c`; by hand `bash scripts/build_dither.sh` |
| Desktop (MSVC 19.44, x64) | `inkframe\render\dither.dll` | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_dither.ps1` |

Both are gitignored. The C is plain C11 and compiles warning-free with
`gcc -std=c11 -Wall -Wextra -Wpedantic -Wconversion -Wshadow` (gcc 14.2,
aarch64) and with MSVC `/W4 /analyze`.

`scripts/dither_stats.py` prints every figure quoted below (spreads,
flat-area checks, grey shares, and with `--lab-cube` the full-cube Lab
accuracy), so they can be re-checked after a change.

## API

```c
int dither_init(void);                     /* once, before anything else */
int dither(const uint8_t *rgb, int w, int h,
           const uint8_t *palette_rgb, int n_colors,
           int method, int serpentine, float strength, int distance_mode,
           const uint8_t *threshold, int threshold_size,
           uint8_t *out_index);
int dither_ordered_spread(const uint8_t *palette_rgb, int n_colors, int distance_mode);
int dither_abi_version(void);              /* 2 */
const char *dither_method_name(int);       /* registry id, NULL if unknown */
int dither_rgb16_to_lab(const int32_t *rgb16, int n, int32_t *lab_out);  /* tests only */
```

| Code | Meaning |
|---|---|
| 0 | success |
| -1 | `rgb`, `palette_rgb` or `out_index` is NULL |
| -2 | `w` or `h` outside 1..16384 |
| -3 | `n_colors` outside 2..16 |
| -4 | unknown method |
| -5 | `distance_mode` not 0 (rgb) or 1 (lab) |
| -6 | `blue_noise` without a 64 x 64 `threshold` |
| -7 | `strength` is NaN (finite values are clamped to 0.5..1.0) |
| -8 | Lab distance before `dither_init()` |
| -12 | allocation failed |

`out_index` is untouched on every error. Nearest-colour ties go to the lower
palette index. Methods: 0 `none`, 1 `floyd_steinberg`, 2 `atkinson`,
3 `jarvis`, 4 `stucki`, 5 `burkes`, 6 `sierra`, 7 `sierra_two_row`,
8 `sierra_lite`, 9 `bayer_4`, 10 `bayer_8`, 11 `blue_noise`.
`dither_ordered_spread()` returns the ordered methods' spread for a palette
(below) in scaled units, or a negative code. In Python it is
`dither.ordered_spread(palette, distance)`, in 8-bit levels.

## Fixed point

No floating point is used in any per-pixel path.

- **Scale.** A channel is `value * 16` (4 fractional bits), so 0..4080.
- **Strength.** `q = (int)(clamp(strength, 0.5f, 1.0f) * 256.0f + 0.5f)`,
  128..256 (0.85 gives 218). The product is exact in binary32, so the single
  rounding of `+ 0.5f` cannot depend on FMA contraction.
- **Error diffusion** (per channel; every `/` is C's truncating division,
  and no negative value is ever right-shifted):

  ```
  v   = clamp(src * 16 + acc / denom, -2040, 6120)    # [-0.5, 1.5] x 255 x 16
  k   = nearest(v)
  e   = (v - palette[k] * 16) * q / 256
  acc[x + dir*dx, y + dy] += e * weight               # every kernel tap
  ```

  The buffer accumulates `error * weight`, and a pixel divides its sum by
  the kernel denominator once, when it is read. That is cheaper, and it loses
  less error than truncating every tap. Bounds: `|e| <= 6120` and
  `|acc| <= 6120 * 48`, well inside int32.
- **Clamp.** The plan asks for "error clamping to +-1.5x the channel range to
  stop runaway". Inkframe clamps the error-adjusted *value* to
  [-0.5, 1.5] x 255, so a pixel can push at most 1.5 x 255 levels of error
  beyond the colour it picked. The other reading, clamping the incoming error
  to +-382 levels, lets the value reach 637 and dumps up to twice the error
  when a saturated area ends. With Floyd-Steinberg at strength 1.0 and the
  measured palette, on 60 x 120 test cards whose left half is pure yellow,
  pure green or pure blue and whose right half is measured blue, red or white,
  the value clamp leaves 52, 58 and 9 stray pixels in the right half and the
  error clamp 86, 88 and 27 (reaching 3 columns in instead of 1 on the green
  card). `test_clamp_is_on_the_value_and_pinned_to_the_unit` pins the choice:
  moving either clamp by one scaled unit, or clamping the error instead,
  changes its output.
- **Buffer.** A ring of three rows of `(w + 4) x 3` int32 (21.7 KB at 600 px).
  Two padding columns per side absorb taps that fall off the edges; taps
  below the last row land in a slot that is never read. Errors that leave the
  image are dropped, never wrapped.
- **Serpentine.** Odd rows scan right to left with every `dx` mirrored.
  Ordered methods and `none` are point-wise, so serpentine has no effect on
  them.
- **Kernels** (weights / denominator) are written out as `TAP()` lines in
  `run_diffusion()`, so the compiler sees constant offsets. That made the
  three-row kernels about 40 % faster on the A53 than a table-driven loop.
  Floyd-Steinberg /16, Atkinson /8 (the weights sum to 6, so only 6/8 of the
  error propagates), Jarvis-Judice-Ninke /48, Stucki /42, Burkes /32,
  Sierra-3 /32, Sierra two-row /16, Sierra Lite /4. `tests/dither_reference.py`
  transcribes the same tables independently.

## Ordered methods: a palette-tied spread

Each cell adds the same offset to all three channels (a step along the grey
axis) before the nearest-colour search:

```
offset = (t - 127.5) / 127.5 * strength * spread              t = threshold 0..255
       = (2t - 255) * q * spread / (255 * 256)                (scaled, truncating)
```

**The spread depends on the palette and the metric.** It is the largest
grey-axis offset, at most 96 levels, that leaves every *anchor* on the
palette entry it maps to without dithering. The anchors are the palette
entries, pure black and pure white. `ordered_spread()` in `dither.c` finds it
once per call by testing every scaled step (1/16 level) in both directions
from every anchor, stopping at the first flip. It does not bisect, because a
Lab cell need not be convex along the sRGB grey axis. Every per-cell offset
is at most the spread, so **a flat area of a palette colour, of pure black or
of pure white stays one colour** for any palette, metric and strength, exactly
as it does with error diffusion and `none`. Strength scales the offsets
within that spread. The scan costs at most 0.7 ms per call on the desktop and
a few ms on the Pi. It is integer-only and uses the same nearest-colour search
as the pixels.

| Palette | rgb | lab | limited by |
|---|---|---|---|
| nominal | 96 | 96 | the cap (uncapped it would be 127.4 in rgb and 118.9 in lab) |
| measured (`MEASURED_DEFAULT`) | 37.3125 | 77.5 | rgb: black flips to green at +37.3 and green to black at -37.3; lab: green to black at -77.5 |

Why the cap is 96 levels: the six-colour palette has gaps of 200 to 255
levels between neighbouring colours (measured black 24 to white 226, nominal
primaries 255). With spread S, a grey ramp dithers only between two colours
within S of their boundary and is flat elsewhere.

- **+-48** dithers only the middle 38 % of a black-white ramp. Photos then
  show large flat posterised blobs. Compared on a test chart.
- **+-128 or more** covers the whole gap, but mid-tones alternate between
  distant colours, which is visibly noisier. It is also past the nominal
  palette's own flip distance (127.4 levels in rgb, 118.9 in lab), so the
  palette-tied limit would cut it back anyway.
- **+-96** covers about 95 % of the measured black-white gap, and every flat
  nominal colour stays itself.

**What a smaller spread costs.** The first version used a fixed +-96 for every
palette. With `MEASURED_DEFAULT` that speckled flat areas of the palette's
own colours, for example 12 % green in pure black at the default strength in
rgb, because the grey-axis offset reaches the dull measured green before any
grey. The palette-tied spread removes that. In exchange, measured rgb
dithers a narrower band around each boundary: on a 0..255 grey ramp, Bayer
8 x 8 mixes colours in 85 of the 256 columns (rgb) and 115 (lab), against 111
for the nominal palette. Measured rgb is a poor metric for this palette
anyway (next section), and the default recipe is `atkinson` with `lab`.

Bayer matrices are built in C from `M(2n) = [[4M, 4M+2], [4M+3, 4M+1]]` and
normalised as `round(v * 255 / (n*n - 1))` (`17 v` for 4 x 4), which is
symmetric around 127.5. `dither.bayer_matrix(n)` returns the same values.
`blue_noise` tiles `blue_noise_64.npy`.

## The measured palette and greys

`MEASURED_DEFAULT` is a starting guess (plan section 6), and its green
(52, 122, 84) is close to the grey axis. So whatever the engine does, some
mid greys are nearer that green than black or white under either metric.
This is a property of the palette values and the metric, not an engine bug,
and error diffusion does **not** fully correct it. Figures for flat greys at
the default strength 0.85:

- `none`, rgb: every grey from 63 to 152 maps to 100 % green. With lab, greys
  102 to 129 do (grey 128 is 100 % green).
- `atkinson`, rgb: grey 128 is 62 % green, 14 % red and 24 % white, with no
  black. Grey 96 is 71 % green.
- `atkinson`, lab (the default recipe): greys 64 to 160 still carry 4 to 16 %
  green (grey 64: 16 %, grey 128: 7 %). `floyd_steinberg`/lab keeps it at
  5 % or less.
- Ordered, lab: 19 to 25 % green across greys 64 to 160. Ordered, rgb: greys
  94 to 120 are 100 % green.

Tuning the measured values from the palette test card (Phase 7) is the real
fix. Until then, prefer `lab`, and error diffusion for photos with large grey
areas.

## Lab distance (`distance="lab"`, CIE76, D65)

Two integer tables are built once in double precision and rounded:

- `g_lin[256]`: 65536 x sRGB-to-linear(i/255).
- `g_flab[4097]`: 65536 x f(j/4096), where f(t) = cbrt(t) above 216/24389,
  and the CIE linear toe below.

Per pixel: clamp each channel to 0..4080, then linearly interpolate `g_lin`
over the 4 fractional bits. Multiply by an integer sRGB-to-XYZ matrix with
the D65 white folded in. It is scaled by 4096 and every row sums to exactly
4096, so white maps exactly to Lab (100, 0, 0). Then interpolate `g_flab`,
and form L\*, a\*, b\* scaled by 256 (L\* 0..25600). Squared distances are
int64.

Clamping applies only to the colour lookup. The diffused error still uses the
unclamped value.

**Accuracy**, over all 2^24 sRGB colours against an independent float64
conversion: max 0.0866 dE (at (253, 0, 3)), mean 0.0176. The test bound is
0.09. The engine's nearest colour differs from float CIE76 for 3730 colours
with the nominal palette and 3582 with the measured one (of 16.7 million).
All of them are near-ties: the engine's pick is at most 0.058 dE (nominal) and
0.070 dE (measured) farther than the float choice.

**Initialisation.** `dither_init()` builds the tables. `load_library()` calls
it once, under its lock, before any other thread can see the library. After
that the tables are only read, so concurrent calls are safe. There is
deliberately no lazy build on first use: a Lab call before `dither_init()`
returns -8. A first-use build guarded by a plain or `volatile` flag would not
order the table stores before the flag on a weakly ordered CPU such as the
Pi's Cortex-A53, and C11 atomics are not available in MSVC without an
experimental switch.

## Why MSVC/x64 and gcc/aarch64 agree bit for bit

1. **Every per-pixel operation is integer arithmetic with fully defined C
   semantics.** C99/C11 define `/` as truncation toward zero, every value is
   bounded (no signed overflow), negative values are never shifted, and
   `int32_t` is exact-width two's complement. The compiler may pick any
   instruction sequence (it turns `/ 256` into shifts with a sign fix-up); the
   result is fixed by the language. The ordered spread scan is integer code
   too.
2. **The strength conversion.** It is one binary32 multiply by a power of two
   (exact) and one rounding. Both targets use IEEE-754 binary32: SSE2 on x64,
   since MSVC never uses x87 for x64; native on AArch64.
3. **The Lab tables are the only libm-dependent values.** That is 4353
   doubles rounded to integers. Two libms (glibc, the Windows UCRT) can only
   disagree on an entry if their `pow`/`cbrt` results differ (by an ulp, about
   1e-16 relative, so about 1e-11 absolute at a scale of 65536) and the exact
   value lies within that distance of a .5 rounding boundary.
   `test_lab_tables_are_far_from_rounding_boundaries` recomputes every entry
   with 50-digit decimals. The closest entry is 2.4e-4 from a boundary,
   seven orders of magnitude more than any libm error, so every
   IEEE-conforming libm, with or without FMA contraction, produces the same
   tables. The piecewise thresholds (c <= 0.04045, t > 216/24389) are not
   borderline for any table input either.
4. **Verified.** `tests/golden/dither_golden.json` holds 120 hashes
   (`tests.gen_dither_golden`: all 12 methods with every knob). It was
   generated on the desktop (MSVC 19.44), and `test_golden_hashes` passes on
   the Pi with gcc 14.2, both with `install.sh`'s flags and with
   `scripts/build_dither.sh` (`-std=c11 -Wall -Wextra`).

Build flags must stay IEEE: no `-ffast-math` or `/fp:fast`.

## Blue noise

`scripts/gen_blue_noise.py` runs Ulichney's void-and-cluster on a 64 x 64
torus (Gaussian sigma 1.9, seed 20261019, 10 % initial ones). It stores
`rank * 256 // 4096` as uint8, so each value occurs exactly 16 times. Below
1/8 cycle per pixel the texture has 0.0002 times the power of white noise
with the same values; the script and `test_blue_noise_texture` both check
this. A missing or malformed texture file raises `DitherError`.

## Performance (600 x 400, best of 3, ms, rgb / lab)

| Method | Desktop (MSVC /O2) | Pi Zero 2 W (gcc -O2) |
|---|---|---|
| none | 1.6 / 3.8 | 24 / 49 |
| floyd_steinberg | 3.6 / 6.9 | 52 / 86 |
| atkinson | 3.8 / 6.9 | 56 / 91 |
| jarvis | 4.5 / 7.7 | 70 / 104 |
| stucki | 4.4 / 7.6 | 69 / 103 |
| burkes | 4.0 / 7.2 | 59 / 93 |
| sierra | 4.2 / 7.5 | 65 / 100 |
| sierra_two_row | 3.9 / 7.2 | 59 / 93 |
| sierra_lite | 3.5 / 6.7 | 50 / 84 |
| bayer_4 / bayer_8 / blue_noise | 2.0 / 5.3 | 27 / 61 |

The ordered rows include the per-call spread scan: at most 0.7 ms on the
desktop, and about 1 ms (rgb) and 4 ms (lab) on the Pi. The targets were
< 40 ms on the desktop and < 150 ms on the Pi. A preview strip of six methods
costs about 0.6 s on the Pi. ctypes releases the GIL during the call.

## Tests

`tests/test_dither.py` covers several layers:

- Exact equality with `tests/dither_reference.py`, a pure-Python
  pixel-by-pixel spec: every kernel, serpentine on and off, three strengths,
  both palettes, rgb and the integer Lab path, and the ordered spread scan.
- Lab accuracy against float, and the table rounding margins.
- Properties: flat palette colours, pure black and pure white stay flat for
  every method, metric and strength, with both palettes; the ordered spread
  values; serpentine and strength sensitivity; determinism; ties.
- The clamp: saturated probes where moving VMIN or VMAX by one unit, or
  clamping the error instead of the value, changes the output.
- Argument errors and C error codes, the `dither_init()` contract (checked on
  a private copy of the library), the blue-noise spectrum, the CLI, and
  timing (< 1 s per method).
- Golden hashes (`tests/golden/dither_golden.json`).

A missing or stale library makes every test in the module fail with the
build command. The failure comes from a fixture, not from import time, so the
rest of the suite still collects and runs.

As a check on the tests themselves, 22 deliberate one-line bugs were each
compiled into a throw-away DLL. Examples: an unmirrored serpentine, `<=` tie
breaking, a signed shift, a wrong tap, a wrong ring-row clear, interpolation
divisors, clamps, a transposed Bayer matrix, and a strength rounding error.
The suite caught every one.

Regenerate the golden file only when output is meant to change:
`python -m tests.gen_dither_golden`. Review the JSON diff like code, and run
`tests/test_dither.py` on the Pi before committing it.

Manual experiments:
`python -m inkframe.render.dither IN.png OUT.png --method atkinson --palette measured --strength 0.85 --distance lab [--no-serpentine] [--fit]`.
A missing or unreadable input or output path is reported as `error: ...`
with exit code 2. 16-bit greyscale input is scaled to 8 bits, not clipped.
