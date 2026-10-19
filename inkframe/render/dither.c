/*
 * dither.c - Inkframe's dither engine: RGB image -> palette indices.
 *
 * Built as a shared library and loaded with ctypes by inkframe/render/dither.py:
 *   Pi (gcc 14, aarch64):  gcc -O2 -shared -fPIC -o inkframe/render/libdither.so \
 *                              inkframe/render/dither.c -lm      (install.sh build_native)
 *   desktop (MSVC 19.44):  scripts/build_dither.ps1  ->  inkframe/render/dither.dll
 * Plain C11 that both compilers accept warning-free (gcc -Wall -Wextra, MSVC /W3).
 *
 * DETERMINISM.  Golden hashes are shared between MSVC/x64 and gcc/aarch64, so
 * the output must be bit-identical on both.  Rules followed throughout:
 *   - no floating point in any per-pixel path: all pixel, error and colour
 *     arithmetic is int32 (int64 only for Lab squared distances);
 *   - every division of a possibly negative value is C's '/', which C99/C11
 *     define as truncation toward zero; no right shift of a negative value;
 *   - every intermediate has a documented bound, so no signed overflow;
 *   - the only float code: strength -> q8 (exact: see dither()) and the Lab
 *     lookup tables, built once by dither_init() in double and rounded to
 *     integers (see build_tables() and docs/DITHER.md for why libm
 *     differences cannot leak).
 *
 * FIXED POINT.  A channel value v is stored scaled by SCALE = 16 (4 fractional
 * bits): 8-bit c becomes c * 16, range 0..4080.  Diffused error is in the same
 * units (the error buffer holds it multiplied by the kernel weights; see
 * run_diffusion()).  Before the nearest-colour search the error-adjusted value
 * is clamped to [-0.5, 1.5] x the channel range = [VMIN, VMAX] = [-2040, 6120],
 * which stops runaway error on saturated input.  This is how Inkframe reads
 * the plan's "error clamping to +-1.5x the channel range": the VALUE is
 * clamped, so the error a pixel can push is at most 1.5 x 255 levels beyond
 * the palette colour it picked.  Clamping the incoming error to +-382 levels
 * instead lets the value reach 637 and bleeds visibly more next to saturated
 * areas (docs/DITHER.md, "Clamp").
 *
 * Floating-point build flags must stay IEEE (no -ffast-math, no /fp:fast):
 * the NaN check on strength and the table rounding rely on it.
 */

#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#if defined(_WIN32)
#define DITHER_API __declspec(dllexport)
#else
#define DITHER_API __attribute__((visibility("default")))
#endif

#define DITHER_ABI_VERSION 2

/* ---- return codes (mirrored in dither.py _ERRORS) ------------------------ */
#define DITHER_OK 0
#define DITHER_E_NULL (-1)      /* rgb, palette_rgb or out_index is NULL        */
#define DITHER_E_SIZE (-2)      /* w or h outside 1..MAX_DIM                    */
#define DITHER_E_PALETTE (-3)   /* n_colors outside 2..MAX_COLORS               */
#define DITHER_E_METHOD (-4)    /* method is not one of the enum values below   */
#define DITHER_E_DISTANCE (-5)  /* distance_mode is not 0 (rgb) or 1 (lab)      */
#define DITHER_E_THRESHOLD (-6) /* blue_noise without a 64x64 threshold matrix  */
#define DITHER_E_STRENGTH (-7)  /* strength is NaN (finite values are clamped)  */
#define DITHER_E_INIT (-8)      /* Lab distance before dither_init() was called */
#define DITHER_E_NOMEM (-12)    /* allocation failed (-ENOMEM on Linux)         */

/* ---- methods (ids are the ABI; dither.py METHODS[i].c_id) ---------------- */
enum {
    M_NONE = 0,
    M_FLOYD_STEINBERG = 1,
    M_ATKINSON = 2,
    M_JARVIS = 3,
    M_STUCKI = 4,
    M_BURKES = 5,
    M_SIERRA = 6,
    M_SIERRA_TWO_ROW = 7,
    M_SIERRA_LITE = 8,
    M_BAYER_4 = 9,
    M_BAYER_8 = 10,
    M_BLUE_NOISE = 11,
    M_COUNT = 12
};

static const char *const METHOD_NAMES[M_COUNT] = {
    "none", "floyd_steinberg", "atkinson", "jarvis", "stucki", "burkes",
    "sierra", "sierra_two_row", "sierra_lite", "bayer_4", "bayer_8", "blue_noise",
};

/* ---- fixed-point constants ----------------------------------------------- */
#define SCALE 16                          /* pixel/error units per 8-bit level */
#define VMAX8 (255 * SCALE)               /* 4080: scaled 8-bit maximum        */
#define VMIN (-(255 * SCALE) / 2)         /* -2040 = -0.5 x 255 x SCALE        */
#define VMAX ((3 * 255 * SCALE) / 2)      /*  6120 = +1.5 x 255 x SCALE        */
#define MAX_DIM 16384
#define MAX_COLORS 16
#define PAD 2                             /* error-row margin: kernels reach x+-2 */
#define BLUE_NOISE_SIZE 64

/*
 * Ordered dithering adds a per-cell offset to all three channels (a step along
 * the grey axis) before the nearest-colour search:
 *   offset = (t - 127.5) / 127.5 * strength * spread     t = threshold 0..255
 * The spread is tied to the palette and the distance mode (ordered_spread()):
 * the largest grey-axis offset, at most ORDERED_SPREAD = 96 levels, that leaves
 * every anchor on the palette entry it maps to without an offset.  The anchors
 * are the palette entries and the black and white corners of the RGB cube, so
 * a flat area of a palette colour, of pure black or of pure white stays flat
 * for any palette, distance and strength.
 *
 * 96 levels is the cap because it suits the nominal palette (its anchors
 * flip only at 127.4 levels in rgb and 118.9 in lab, so it gets the full 96):
 * a grey ramp maps to an almost linear mix instead of posterised bands, and
 * more adds visible noise.  A palette whose colours sit closer together gets
 * less: MEASURED_DEFAULT gets 37.3 levels in rgb (its black and green are
 * close along the grey axis) and 77.5 in lab.  See docs/DITHER.md.
 *
 * In scaled integers (q = strength * 256, spread in scaled units 0..SPREAD_MAX):
 *   offset_scaled = (2t - 255) * q * spread / (255 * 256),  |offset| <= spread
 * |numerator| <= 255 * 256 * 1536 = 100,270,080 < 2^31.
 */
#define ORDERED_SPREAD 96                 /* cap, in 8-bit levels               */
#define SPREAD_MAX (ORDERED_SPREAD * SCALE) /* 1536 scaled units                */

/* ---- error-diffusion kernels --------------------------------------------- */
/*
 * Notation "(dx, dy) weight ... / denominator": the pixel at (x + dir * dx,
 * y + dy) receives weight / denominator of the error, where dir is +1 on
 * left-to-right rows and -1 on the right-to-left rows of a serpentine scan
 * (the kernel is mirrored).  The weights are the TAP() lines in
 * run_diffusion(), written out as code so the compiler sees constant offsets
 * and weights; this function gives the denominators.
 */
static int32_t kernel_denominator(int method)
{
    switch (method) {
    case M_FLOYD_STEINBERG: return 16;
    case M_ATKINSON: return 8; /* its weights sum to 6: only 6/8 propagates */
    case M_JARVIS: return 48;
    case M_STUCKI: return 42;
    case M_BURKES: return 32;
    case M_SIERRA: return 32;
    case M_SIERRA_TWO_ROW: return 16;
    case M_SIERRA_LITE: return 4;
    default: return 0;
    }
}

static inline int32_t clampi(int32_t v, int32_t lo, int32_t hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

/* ---- CIE L*a*b* (D65) in integers ---------------------------------------- */
/*
 * Tables, built once in double precision and rounded to the nearest integer:
 *   g_lin[i]  = 65536 * sRGB-to-linear(i / 255),                    i = 0..255
 *   g_flab[j] = 65536 * f(j / 4096), f(t) = cbrt(t) for t > 216/24389,
 *               else (24389/27 * t + 16) / 116  (the CIE linear toe), j = 0..4096
 * Per pixel: linear = g_lin interpolated over the 4 fractional bits of the
 * scaled value; XYZ/white = LAB_M (below) x linear; f() interpolated from
 * g_flab; L*, a*, b* scaled by 256 (L* 0..100 -> 0..25600).
 *
 * Initialisation: dither_init() builds the tables.  Call it once, before any
 * Lab call and before more than one thread uses the library; dither.py's
 * load_library() does so under its load lock, before it hands the library
 * out.  Afterwards the tables are only ever read, so concurrent calls are
 * safe.  There is deliberately no lazy build on first use: a Lab call before
 * dither_init() returns DITHER_E_INIT, so no thread can ever observe
 * g_tables_ready set before the table stores (which a plain or volatile flag
 * would not order on weakly ordered CPUs such as the Pi's Cortex-A53).
 */
#define LAB_ONE 65536 /* 1.0 for linear light, XYZ/white and f() */
static int32_t g_lin[256];
static int32_t g_flab[4097];
static int g_tables_ready = 0; /* written only by dither_init() */

/* sRGB (IEC 61966-2-1) linear RGB -> XYZ, each row divided by the D65 white
 * (0.95047, 1.0, 1.08883) and scaled by 4096; rows sum to exactly 4096, so
 * white maps to XYZ/white = (1, 1, 1) and Lab (100, 0, 0) exactly. */
static const int32_t LAB_M[3][3] = {
    {1777, 1541, 778},
    {871, 2929, 296},
    {73, 448, 3575},
};

static void build_tables(void)
{
    int i;
    for (i = 0; i < 256; ++i) {
        double c = (double)i / 255.0;
        double lin = (c <= 0.04045) ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4);
        g_lin[i] = (int32_t)floor(lin * 65536.0 + 0.5);
    }
    for (i = 0; i <= 4096; ++i) {
        double t = (double)i / 4096.0;
        double f = (t > 216.0 / 24389.0) ? cbrt(t) : ((24389.0 / 27.0) * t + 16.0) / 116.0;
        g_flab[i] = (int32_t)floor(f * 65536.0 + 0.5);
    }
    g_tables_ready = 1;
}

/* v: scaled channel 0..VMAX8 -> linear light 0..65536.  Both tables are
 * monotonic, so interpolation steps are non-negative and unsigned arithmetic
 * (plain shifts, no sign fix-ups) is exact: (<= 585 * 15) / 16. */
static inline int32_t lin_of(int32_t v)
{
    uint32_t u = (uint32_t)v, i = u / SCALE, frac = u % SCALE; /* v >= 0 */
    if (frac == 0) {
        return g_lin[i]; /* also the only way to reach i = 255 */
    }
    return g_lin[i] + (int32_t)(((uint32_t)(g_lin[i + 1] - g_lin[i]) * frac) / SCALE);
}

/* t: XYZ/white component 0..65536 (clamped) -> f(t) scaled by 65536. */
static inline int32_t flab_of(int32_t t)
{
    uint32_t i, frac;
    if (t <= 0) {
        return g_flab[0];
    }
    if (t >= LAB_ONE) {
        return g_flab[4096];
    }
    i = (uint32_t)t / 16u; /* 0..4095: 4096 intervals of 16 units */
    frac = (uint32_t)t % 16u;
    return g_flab[i] + (int32_t)(((uint32_t)(g_flab[i + 1] - g_flab[i]) * frac) / 16u);
}

/* Scaled sRGB (each 0..VMAX8) -> L*, a*, b* x 256.  Bounds: products
 * <= 4096 * 65536 = 2^28; 116 * 65536, 500 * 65536 < 2^25. */
static inline void lab_of(int32_t r, int32_t g, int32_t b, int32_t lab[3])
{
    int32_t lr = lin_of(r), lg = lin_of(g), lb = lin_of(b);
    int32_t x = (LAB_M[0][0] * lr + LAB_M[0][1] * lg + LAB_M[0][2] * lb + 2048) / 4096;
    int32_t y = (LAB_M[1][0] * lr + LAB_M[1][1] * lg + LAB_M[1][2] * lb + 2048) / 4096;
    int32_t z = (LAB_M[2][0] * lr + LAB_M[2][1] * lg + LAB_M[2][2] * lb + 2048) / 4096;
    int32_t fx = flab_of(x), fy = flab_of(y), fz = flab_of(z);
    lab[0] = (116 * fy - 16 * LAB_ONE) / 256; /* (116 f(y) - 16) * 256 */
    lab[1] = (500 * (fx - fy)) / 256;
    lab[2] = (200 * (fy - fz)) / 256;
}

/* Any scaled value (error diffusion can leave the sRGB cube) -> Lab of the
 * nearest in-cube colour: each channel clamped to 0..VMAX8 first. */
static inline void lab_of_clamped(int32_t r, int32_t g, int32_t b, int32_t lab[3])
{
    lab_of(clampi(r, 0, VMAX8), clampi(g, 0, VMAX8), clampi(b, 0, VMAX8), lab);
}

/* ---- nearest palette colour ---------------------------------------------- */
typedef struct {
    int n;                          /* palette size, 2..MAX_COLORS           */
    int lab;                        /* 0: Euclidean sRGB, 1: CIE76 in Lab    */
    int32_t pal[MAX_COLORS][3];     /* palette RGB scaled by SCALE           */
    int32_t pal_lab[MAX_COLORS][3]; /* palette L*a*b* x 256 (lab mode only)  */
} ctx_t;

/* Squared Euclidean distance in scaled sRGB.  Inputs are in [VMIN, VMAX], the
 * palette in [0, VMAX8], so |d| <= 6120 and the sum <= 3 * 6120^2 = 112,363,200
 * < INT32_MAX.  Ties go to the LOWER palette index (strict '<'). */
static inline int nearest_rgb(const ctx_t *c, int32_t v0, int32_t v1, int32_t v2)
{
    int k, best = 0;
    int32_t bestd = INT32_MAX;
    for (k = 0; k < c->n; ++k) {
        int32_t d0 = v0 - c->pal[k][0];
        int32_t d1 = v1 - c->pal[k][1];
        int32_t d2 = v2 - c->pal[k][2];
        int32_t d = d0 * d0 + d1 * d1 + d2 * d2;
        if (d < bestd) {
            bestd = d;
            best = k;
        }
    }
    return best;
}

/* CIE76: squared Euclidean distance in L*a*b* x 256 (int64: differences reach
 * ~2^16).  Values outside the sRGB cube (possible after error diffusion) are
 * clamped to it for the colour lookup only; the diffused error still uses the
 * unclamped value.  Ties go to the LOWER palette index. */
static inline int nearest_lab(const ctx_t *c, int32_t v0, int32_t v1, int32_t v2)
{
    int k, best = 0;
    int64_t bestd = INT64_MAX;
    int32_t lab[3];
    lab_of_clamped(v0, v1, v2, lab);
    for (k = 0; k < c->n; ++k) {
        int64_t dl = (int64_t)(lab[0] - c->pal_lab[k][0]);
        int64_t da = (int64_t)(lab[1] - c->pal_lab[k][1]);
        int64_t db = (int64_t)(lab[2] - c->pal_lab[k][2]);
        int64_t d = dl * dl + da * da + db * db;
        if (d < bestd) {
            bestd = d;
            best = k;
        }
    }
    return best;
}

/* Either metric, for the set-up code (the per-pixel loops pick it inline). */
static int nearest_any(const ctx_t *c, int32_t v0, int32_t v1, int32_t v2)
{
    return c->lab ? nearest_lab(c, v0, v1, v2) : nearest_rgb(c, v0, v1, v2);
}

/* ---- point-wise methods: none and ordered -------------------------------- */
/*
 * The ordered methods' spread for this palette and metric, in scaled units
 * 0..SPREAD_MAX: the largest S such that for every anchor a and EVERY integer
 * offset o in [-S, S], nearest(a + (o, o, o)) == nearest(a).  Anchors: each
 * palette entry, (0, 0, 0) and (255, 255, 255).  Every scaled step is tested,
 * not bisected (a Lab cell need not be convex along the sRGB grey axis), and
 * build_offsets() never makes |offset| > S, so a flat anchor colour provably
 * stays one colour.  p in [0, VMAX8] and o <= SPREAD_MAX keep p +- o inside
 * [VMIN, VMAX], so no clamp is needed.  Cost: at most SPREAD_MAX * (n + 2) * 2
 * searches (24,576 for six colours; ~1 ms on the desktop in Lab), and it stops
 * at the first flip.  Integer-only, like the per-pixel paths.
 */
static int32_t ordered_spread(const ctx_t *c)
{
    int32_t anchor[MAX_COLORS + 2][3];
    int home[MAX_COLORS + 2];
    int n_anchor = c->n + 2, a, ch;
    int32_t o;

    for (a = 0; a < c->n; ++a) {
        for (ch = 0; ch < 3; ++ch) {
            anchor[a][ch] = c->pal[a][ch];
        }
    }
    for (ch = 0; ch < 3; ++ch) {
        anchor[c->n][ch] = 0;         /* black corner */
        anchor[c->n + 1][ch] = VMAX8; /* white corner */
    }
    for (a = 0; a < n_anchor; ++a) {
        home[a] = nearest_any(c, anchor[a][0], anchor[a][1], anchor[a][2]);
    }
    for (o = 1; o <= SPREAD_MAX; ++o) {
        for (a = 0; a < n_anchor; ++a) {
            const int32_t *p = anchor[a];
            if (nearest_any(c, p[0] + o, p[1] + o, p[2] + o) != home[a] ||
                nearest_any(c, p[0] - o, p[1] - o, p[2] - o) != home[a]) {
                return o - 1;
            }
        }
    }
    return SPREAD_MAX;
}

/* offs: n_off x n_off scaled offsets tiled over the image (NULL = method none).
 * Every pixel is independent, so serpentine has no effect here. */
static void run_pointwise(const uint8_t *rgb, int w, int h, const ctx_t *c,
                          const int32_t *offs, int n_off, uint8_t *out)
{
    int x, y;
    for (y = 0; y < h; ++y) {
        const uint8_t *src = rgb + (size_t)y * (size_t)w * 3;
        uint8_t *dst = out + (size_t)y * (size_t)w;
        const int32_t *orow = offs ? offs + (size_t)(y % n_off) * (size_t)n_off : NULL;
        for (x = 0; x < w; ++x) {
            int32_t o = orow ? orow[x % n_off] : 0;
            /* |o| <= SPREAD_MAX, so these clamps never bind; kept for symmetry */
            int32_t v0 = clampi(src[3 * x + 0] * SCALE + o, VMIN, VMAX);
            int32_t v1 = clampi(src[3 * x + 1] * SCALE + o, VMIN, VMAX);
            int32_t v2 = clampi(src[3 * x + 2] * SCALE + o, VMIN, VMAX);
            int idx = c->lab ? nearest_lab(c, v0, v1, v2) : nearest_rgb(c, v0, v1, v2);
            dst[x] = (uint8_t)idx;
        }
    }
}

/* Bayer matrix entry (0..n*n-1) from the recursive definition
 * M(2n) = [[4M, 4M+2], [4M+3, 4M+1]]: the lowest bits of (x, y) pick the most
 * significant base-4 digit, digit = 2 * (x ^ y) + y per bit. */
static int bayer_value(int x, int y, int log2n)
{
    int bit, v = 0;
    for (bit = 0; bit < log2n; ++bit) {
        int xb = (x >> bit) & 1, yb = (y >> bit) & 1; /* x, y >= 0 */
        v = v * 4 + 2 * (xb ^ yb) + yb;
    }
    return v;
}

/* Fill offs (n x n) for strength q and spread (scaled, 0..SPREAD_MAX).
 * thr != NULL: an n x n 0..255 matrix (blue noise).  thr == NULL: Bayer n x n
 * normalised to 0..255 as t = round(v * 255 / (n*n - 1)) (half up; exact
 * 17 * v for n = 4). */
static void build_offsets(int32_t *offs, int n, const uint8_t *thr, int log2n, int32_t q,
                          int32_t spread)
{
    int x, y;
    int32_t nn1 = (int32_t)(n * n - 1);
    for (y = 0; y < n; ++y) {
        for (x = 0; x < n; ++x) {
            int32_t t;
            if (thr) {
                t = thr[y * n + x];
            } else {
                t = ((int32_t)bayer_value(x, y, log2n) * 510 + nn1) / (2 * nn1);
            }
            offs[y * n + x] = ((2 * t - 255) * q * spread) / (255 * 256);
        }
    }
}

/* ---- error diffusion ------------------------------------------------------ */
/*
 * Error buffer: a ring of 3 rows (y, y+1, y+2), each (w + 2*PAD) pixels x 3
 * channels of int32 (600 px wide: 21.7 KB).  The PAD columns on either side
 * absorb taps that fall off the left/right edge and are never read; taps on
 * rows below the image land in a ring row that is never read.  So errors that
 * leave the image are dropped, never wrapped.
 *
 * The buffer accumulates error x weight; the division by the kernel
 * denominator happens once, when the pixel is read.  Per pixel, channel c:
 *   v[c] = clamp(src[c] * SCALE + acc[y][x][c] / denom, VMIN, VMAX)
 *   k    = nearest(v)                                (ties: lowest index)
 *   e[c] = (v[c] - pal[k][c]) * q / 256              (q = strength * 256)
 *   acc[y + dy][x + dir * dx][c] += e[c] * weight    (every tap)
 * Every '/' truncates toward zero.  Bounds: |v - pal| <= 6120, so
 * |e * q| <= 1,566,720 and |e| <= 6120; a cell collects at most
 * sum(weights) <= denom times max |e|, so |acc| <= 6120 * 48 = 293,760 and
 * |acc / denom| <= 6120.
 */

/* TAP(row, dx, wt): add wt x the error to pixel x + dir * dx of `row`
 * (r0/r1/r2 point at column 0 of rows y/y+1/y+2; d3 = 3 * dir). */
#define TAP(row, dx, wt)                       \
    do {                                       \
        int32_t *tp_ = (row) + x3 + (dx) * d3; \
        tp_[0] += e0 * (wt);                   \
        tp_[1] += e1 * (wt);                   \
        tp_[2] += e2 * (wt);                   \
    } while (0)

static int run_diffusion(const uint8_t *rgb, int w, int h, const ctx_t *c,
                         int method, int serpentine, int32_t q, uint8_t *out)
{
    size_t row_len = (size_t)(w + 2 * PAD) * 3;
    int32_t *buf = (int32_t *)calloc(3 * row_len, sizeof(int32_t));
    int32_t denom = kernel_denominator(method);
    int y;

    if (buf == NULL) {
        return DITHER_E_NOMEM;
    }
    for (y = 0; y < h; ++y) {
        int rev = serpentine && (y & 1); /* odd rows right-to-left */
        int d3 = rev ? -3 : 3;           /* int32 step to the next pixel */
        int32_t *r0 = buf + (size_t)(y % 3) * row_len + 3 * PAD;
        int32_t *r1 = buf + (size_t)((y + 1) % 3) * row_len + 3 * PAD;
        int32_t *r2 = buf + (size_t)((y + 2) % 3) * row_len + 3 * PAD;
        const uint8_t *src_row = rgb + (size_t)y * (size_t)w * 3;
        uint8_t *dst_row = out + (size_t)y * (size_t)w;
        int i;
        /* the ring slot of row y+2 last held row y-1, which is finished */
        memset(r2 - 3 * PAD, 0, row_len * sizeof(int32_t));

        for (i = 0; i < w; ++i) {
            int x = rev ? w - 1 - i : i;
            int x3 = 3 * x, idx;
            const uint8_t *src = src_row + x3;
            const int32_t *cur = r0 + x3;
            int32_t v0 = clampi(src[0] * SCALE + cur[0] / denom, VMIN, VMAX);
            int32_t v1 = clampi(src[1] * SCALE + cur[1] / denom, VMIN, VMAX);
            int32_t v2 = clampi(src[2] * SCALE + cur[2] / denom, VMIN, VMAX);
            int32_t e0, e1, e2;

            idx = c->lab ? nearest_lab(c, v0, v1, v2) : nearest_rgb(c, v0, v1, v2);
            dst_row[x] = (uint8_t)idx;
            e0 = ((v0 - c->pal[idx][0]) * q) / 256;
            e1 = ((v1 - c->pal[idx][1]) * q) / 256;
            e2 = ((v2 - c->pal[idx][2]) * q) / 256;
            if ((e0 | e1 | e2) == 0) {
                continue; /* exact match: nothing to spread */
            }
            switch (method) {
            case M_FLOYD_STEINBERG: /* (1,0)7 (-1,1)3 (0,1)5 (1,1)1 /16 */
                TAP(r0, 1, 7);
                TAP(r1, -1, 3); TAP(r1, 0, 5); TAP(r1, 1, 1);
                break;
            case M_ATKINSON: /* (1,0)1 (2,0)1 (-1,1)1 (0,1)1 (1,1)1 (0,2)1 /8 */
                TAP(r0, 1, 1); TAP(r0, 2, 1);
                TAP(r1, -1, 1); TAP(r1, 0, 1); TAP(r1, 1, 1);
                TAP(r2, 0, 1);
                break;
            case M_JARVIS: /* Jarvis-Judice-Ninke /48 */
                TAP(r0, 1, 7); TAP(r0, 2, 5);
                TAP(r1, -2, 3); TAP(r1, -1, 5); TAP(r1, 0, 7); TAP(r1, 1, 5); TAP(r1, 2, 3);
                TAP(r2, -2, 1); TAP(r2, -1, 3); TAP(r2, 0, 5); TAP(r2, 1, 3); TAP(r2, 2, 1);
                break;
            case M_STUCKI: /* /42 */
                TAP(r0, 1, 8); TAP(r0, 2, 4);
                TAP(r1, -2, 2); TAP(r1, -1, 4); TAP(r1, 0, 8); TAP(r1, 1, 4); TAP(r1, 2, 2);
                TAP(r2, -2, 1); TAP(r2, -1, 2); TAP(r2, 0, 4); TAP(r2, 1, 2); TAP(r2, 2, 1);
                break;
            case M_BURKES: /* /32 */
                TAP(r0, 1, 8); TAP(r0, 2, 4);
                TAP(r1, -2, 2); TAP(r1, -1, 4); TAP(r1, 0, 8); TAP(r1, 1, 4); TAP(r1, 2, 2);
                break;
            case M_SIERRA: /* Sierra-3 /32 */
                TAP(r0, 1, 5); TAP(r0, 2, 3);
                TAP(r1, -2, 2); TAP(r1, -1, 4); TAP(r1, 0, 5); TAP(r1, 1, 4); TAP(r1, 2, 2);
                TAP(r2, -1, 2); TAP(r2, 0, 3); TAP(r2, 1, 2);
                break;
            case M_SIERRA_TWO_ROW: /* /16 */
                TAP(r0, 1, 4); TAP(r0, 2, 3);
                TAP(r1, -2, 1); TAP(r1, -1, 2); TAP(r1, 0, 3); TAP(r1, 1, 2); TAP(r1, 2, 1);
                break;
            default: /* M_SIERRA_LITE: (1,0)2 (-1,1)1 (0,1)1 /4 */
                TAP(r0, 1, 2);
                TAP(r1, -1, 1); TAP(r1, 0, 1);
                break;
            }
        }
    }
    free(buf);
    return DITHER_OK;
}

#undef TAP

/* ---- public API ------------------------------------------------------------ */

/* Palette (already validated) -> ctx.  Lab mode needs the tables built. */
static void init_ctx(ctx_t *c, const uint8_t *palette_rgb, int n_colors, int lab)
{
    int k;
    c->n = n_colors;
    c->lab = lab;
    for (k = 0; k < n_colors; ++k) {
        c->pal[k][0] = palette_rgb[3 * k + 0] * SCALE;
        c->pal[k][1] = palette_rgb[3 * k + 1] * SCALE;
        c->pal[k][2] = palette_rgb[3 * k + 2] * SCALE;
    }
    if (lab) {
        for (k = 0; k < n_colors; ++k) {
            lab_of(c->pal[k][0], c->pal[k][1], c->pal[k][2], c->pal_lab[k]);
        }
    }
}

/*
 * int dither_init(void)
 *
 * Builds the Lab lookup tables.  Call once after loading the library, before
 * any other call and before more than one thread uses it (see build_tables());
 * later calls do nothing.  Returns DITHER_OK.
 */
DITHER_API int dither_init(void)
{
    if (!g_tables_ready) {
        build_tables();
    }
    return DITHER_OK;
}

/*
 * int dither(rgb, w, h, palette_rgb, n_colors, method, serpentine, strength,
 *            distance_mode, threshold, threshold_size, out_index)
 *
 *   rgb            h * w * 3 bytes, row-major, R G B per pixel
 *   w, h           image size, each 1..16384
 *   palette_rgb    n_colors * 3 bytes; n_colors 2..16
 *   method         0 none (nearest colour), 1 floyd_steinberg, 2 atkinson,
 *                  3 jarvis, 4 stucki, 5 burkes, 6 sierra, 7 sierra_two_row,
 *                  8 sierra_lite, 9 bayer_4, 10 bayer_8, 11 blue_noise
 *   serpentine     non-zero: odd rows scan right-to-left with the kernel
 *                  mirrored (error diffusion only; no effect otherwise)
 *   strength       error-diffusion multiplier / ordered amplitude (a fraction
 *                  of the palette's spread, see ordered_spread()); clamped to
 *                  [0.5, 1.0]; NaN is an error.  Ignored by method none.
 *   distance_mode  0: Euclidean in sRGB, 1: CIE76 dE in L*a*b* (D65; needs
 *                  dither_init() first)
 *   threshold      blue_noise: REQUIRED 64x64 matrix of 0..255, row-major,
 *                  tiled over the image.  Ignored by every other method (the
 *                  Bayer matrices are built here).  May be NULL otherwise.
 *   threshold_size side of the threshold matrix (must be 64 for blue_noise)
 *   out_index      h * w bytes, receives palette indices 0..n_colors-1
 *
 * Returns DITHER_OK (0) or a negative DITHER_E_* code (top of this file); on
 * any error out_index is left untouched (arguments are checked and every
 * buffer allocated before the first pixel is written).  Nearest-colour ties go
 * to the lower palette index.  Re-entrant and thread-safe once dither_init()
 * has run.
 */
DITHER_API int dither(const uint8_t *rgb, int w, int h,
                      const uint8_t *palette_rgb, int n_colors,
                      int method, int serpentine, float strength, int distance_mode,
                      const uint8_t *threshold, int threshold_size,
                      uint8_t *out_index)
{
    ctx_t c;
    int32_t q;

    if (rgb == NULL || palette_rgb == NULL || out_index == NULL) {
        return DITHER_E_NULL;
    }
    if (w < 1 || h < 1 || w > MAX_DIM || h > MAX_DIM) {
        return DITHER_E_SIZE;
    }
    if (n_colors < 2 || n_colors > MAX_COLORS) {
        return DITHER_E_PALETTE;
    }
    if (method < 0 || method >= M_COUNT) {
        return DITHER_E_METHOD;
    }
    if (distance_mode != 0 && distance_mode != 1) {
        return DITHER_E_DISTANCE;
    }
    if (strength != strength) {
        return DITHER_E_STRENGTH; /* NaN */
    }
    if (method == M_BLUE_NOISE && (threshold == NULL || threshold_size != BLUE_NOISE_SIZE)) {
        return DITHER_E_THRESHOLD;
    }
    if (distance_mode == 1 && !g_tables_ready) {
        return DITHER_E_INIT;
    }

    /* strength -> q8 in 128..256.  strength * 256.0f is exact in binary32
     * (power-of-two scale), so the single rounding of "+ 0.5f" is the same with
     * or without FMA contraction, on SSE2 and on AArch64. */
    if (strength < 0.5f) {
        strength = 0.5f;
    }
    if (strength > 1.0f) {
        strength = 1.0f;
    }
    q = (int32_t)(strength * 256.0f + 0.5f);

    init_ctx(&c, palette_rgb, n_colors, distance_mode);

    if (method == M_NONE) {
        run_pointwise(rgb, w, h, &c, NULL, 0, out_index);
        return DITHER_OK;
    }
    if (method == M_BAYER_4 || method == M_BAYER_8 || method == M_BLUE_NOISE) {
        int n = (method == M_BLUE_NOISE) ? BLUE_NOISE_SIZE : (method == M_BAYER_4) ? 4 : 8;
        int log2n = (method == M_BAYER_4) ? 2 : 3; /* unused for blue noise */
        /* heap, not stack: blue noise needs 16 KB */
        int32_t *offs = (int32_t *)malloc((size_t)n * (size_t)n * sizeof(int32_t));
        if (offs == NULL) {
            return DITHER_E_NOMEM;
        }
        build_offsets(offs, n, (method == M_BLUE_NOISE) ? threshold : NULL, log2n, q,
                      ordered_spread(&c));
        run_pointwise(rgb, w, h, &c, offs, n, out_index);
        free(offs);
        return DITHER_OK;
    }
    return run_diffusion(rgb, w, h, &c, method, serpentine != 0, q, out_index);
}

/* ABI version of this library; dither.py refuses any other value. */
DITHER_API int dither_abi_version(void)
{
    return DITHER_ABI_VERSION;
}

/* Registry id of a method ("none", "floyd_steinberg", ...), NULL if unknown. */
DITHER_API const char *dither_method_name(int method)
{
    if (method < 0 || method >= M_COUNT) {
        return NULL;
    }
    return METHOD_NAMES[method];
}

/*
 * int dither_ordered_spread(palette_rgb, n_colors, distance_mode)
 *
 * The spread the ordered methods use for this palette and distance mode at
 * strength 1.0 (see ordered_spread()), in scaled units: 8-bit levels x 16,
 * 0..1536.  Returns it, or DITHER_E_NULL, DITHER_E_PALETTE, DITHER_E_DISTANCE
 * or DITHER_E_INIT (Lab before dither_init()).
 */
DITHER_API int dither_ordered_spread(const uint8_t *palette_rgb, int n_colors, int distance_mode)
{
    ctx_t c;
    if (palette_rgb == NULL) {
        return DITHER_E_NULL;
    }
    if (n_colors < 2 || n_colors > MAX_COLORS) {
        return DITHER_E_PALETTE;
    }
    if (distance_mode != 0 && distance_mode != 1) {
        return DITHER_E_DISTANCE;
    }
    if (distance_mode == 1 && !g_tables_ready) {
        return DITHER_E_INIT;
    }
    init_ctx(&c, palette_rgb, n_colors, distance_mode);
    return (int)ordered_spread(&c);
}

/* Diagnostic hook for the tests: n colours as scaled channels (8-bit value x
 * SCALE, i.e. 4 fractional bits, any int32 as error diffusion can produce)
 * -> exactly the L*, a*, b* x 256 the nearest-colour search uses, through the
 * same lab_of_clamped() (n * 3 int32).  Returns 0, or DITHER_E_NULL /
 * DITHER_E_SIZE (n < 1) / DITHER_E_INIT (before dither_init()). */
DITHER_API int dither_rgb16_to_lab(const int32_t *rgb16, int n, int32_t *lab_out)
{
    int i;
    if (rgb16 == NULL || lab_out == NULL) {
        return DITHER_E_NULL;
    }
    if (n < 1) {
        return DITHER_E_SIZE;
    }
    if (!g_tables_ready) {
        return DITHER_E_INIT;
    }
    for (i = 0; i < n; ++i) {
        lab_of_clamped(rgb16[3 * i], rgb16[3 * i + 1], rgb16[3 * i + 2], lab_out + 3 * i);
    }
    return DITHER_OK;
}
