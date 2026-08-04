# Benchmarks

All measurements on real widefield frames from the Steinmetz Lab share, 16-bit. Throughput is
single-core unless stated. Scripts in [`../scripts/`](../scripts/).

**Caveats, so these numbers are read for what they are.** The per-frame image codecs (JPEG-LS,
JPEG-XL, JPEG-2000, PNG, TIFF) were round-trip verified with `np.array_equal` on every frame. The
**byte-shuffled zstd rows were not** — the inverse shuffle was never executed, so those figures are
compressed sizes only. They are also not like-for-like with the image codecs: zstd sees a whole
multi-frame block and can exploit cross-frame redundancy, while the image codecs encode each frame
independently to preserve random access. Container, index and transform metadata are excluded
throughout. Single runs, no repetitions or confidence intervals, and one early zstd CLI measurement
used `-T0` despite the column heading saying single-core. The sampling is narrow: the shootout is
64 frames from one session, and the per-session table is four sessions.

## Codec shootout

64 frames of 560×560 from `AL_0033 2025-03-05` (no bit shift, 12-bit payload):

| approach | ratio | saved | enc MB/s | dec MB/s |
|---|---|---|---|---|
| zstd-3, raw bytes | 1.48 | 32 % | 40 | 229 |
| TIFF LZW + predictor-2 | 1.48 | 32 % | 50 | 141 |
| PNG level-6 | 1.70 | 41 % | 14 | 62 |
| TIFF deflate + predictor-2 | 1.69 | 41 % | 45 | 143 |
| TIFF zstd + predictor-2 | 1.70 | 41 % | 44 | 82 |
| zstd-3 + byte-plane shuffle | 1.81 | 45 % | 160 | — |
| zstd-19 + shuffle + same-channel delta | 2.01 | 50 % | 2.5 | — |
| JPEG-2000 reversible | 2.35 | 57 % | 6.5 | 8.4 |
| JPEG-XL lossless, effort 3 | 2.36 | 58 % | 10 | 16 |
| JPEG-XL lossless, effort 7 | 2.37 | 58 % | 0.8 | 11 |
| **JPEG-LS** | **2.37** | **58 %** | **30** | **39** |

JPEG-XL matches JPEG-LS on ratio and is 3× slower to encode; at 170 TB that is days of extra wall
clock for nothing. JPEG-LS wins on the product of ratio and speed.

## The bit-shift finding

Sampling 3 frames from each of 90 random archives:

| flavour | low bits always zero | payload bits | n |
|---|---|---|---|
| basler-tiff | 4 | 12 | 54 |
| basler-tiff | 4 | 11 | 21 |
| basler-tiff | 4 | 10 | 7 |
| basler-tiff | 4 | 9 | 3 |
| frame-N | 0 | 11 | 5 |

**85 of 90 archives store their samples left-shifted by 4 bits.** The camera's 9–12 bit output sits
in bits 4..15 with the bottom nibble hard zero; one session used only 808 distinct values, spaced
exactly 16 apart. Every JPEG-LS prediction residual then comes out a multiple of 16 and the codec
pays for four guaranteed-zero LSBs per sample:

| `ZYE_0095 2025-07-12` | ratio |
|---|---|
| JPEG-LS, as stored | 1.63 |
| **JPEG-LS, after `>> 4`** | **2.76** |

Detecting and stripping the shift is a few lines and exactly invertible. It is worth more than every
other optimisation in this document combined.

The shift is **not** a property of the flavour — `AL_0048` is `basler-tiff` with shift 0 — and
payload width varies 9–12 bits, so it must be detected per archive, never assumed.

## Per-session results

After bit-shift normalisation:

| session | flavour | shift | payload | JPEG-LS | + mean image subtracted |
|---|---|---|---|---|---|
| AL_0033 2025-03-05 | frame-N | 0 | 12 bit | 2.37 | 2.44 (−2.8 %) |
| AL_0048 2026-07-01 | frame-N | 0 | 12 bit | 2.57 | 2.62 (−1.9 %) |
| ZYE_0095 2025-07-12 | basler | 4 | 10 bit | 2.76 | 2.81 (−1.7 %) |
| FD_010 2026-02-23 | basler | 4 | 10 bit | 2.88 | — |

## Things that sound promising and are not

**Subtracting the mean image: ~2 %, and a block-local mean is net negative.** The intuition — "the
data is subtle variation around a large mean" — is right, but the codec already exploits it. A
*scalar* mean removal changes the output by **exactly 0.0 %**: JPEG-LS is shift-invariant, so the DC
level is free to begin with. The residual 2 % comes only from removing the mean image's *spatial*
structure (vessels, brain edge, illumination roll-off), and the MED predictor — which predicts each
pixel from its left, upper and upper-left neighbours — already removes anything spatially smooth.

The measured residual sizes above **exclude the mean images needed to invert the transform**, which
changes the conclusion for the block-local variant. One session-wide mean is amortised over every
frame and costs nothing worth counting, so the ~2 % stands. A fresh full-resolution mean every 40
frames is one extra image per 40 — about **2.5 % of the payload, against the 0.4 % it gains**. Doing
it that way makes the file *bigger*. Only the session-wide mean is worth considering at all, and at
2 % it is still not worth a second way for a session to fail.

**Temporal differencing: −4.5 %, i.e. worse.** Differencing two noisy frames doubles the noise
variance, and the result has no spatial smoothness left for the predictor to use. True for
same-channel (step-2) deltas, naive step-1 deltas, and mean-subtraction-then-delta alike. Delta only
helps the weaker compressors (zstd 1.81 → 1.91).

**De-interleaving blue/violet: nothing** (zstd 1.81 → 1.80).

**12-bit repacking as a pre-step: worse.** Mechanically saves 25 % but entropy-codes badly
afterwards (zstd-3 on packed data: 1.43, vs 1.81 on byte-shuffled).

**JPEG-LS inside a TIFF: 1.96 vs 2.37 bare**, probably a `bitspersample` difference in how
tifffile drives the encoder.

## Reproducibility

These scripts do not regenerate the tables from a clean checkout. They read hard-coded `Y:` session
paths, write to hard-coded `D:` paths, and the earliest of them (`bench.py` through `bench7.py`)
add a `sys.path` entry pointing at a scratch directory that no longer exists, because they predate
the package. `bench2.py` additionally reports blue-only and violet-only ratios against the full
two-channel byte count, overstating both by 2x; those rows were never published here. Treat the
scripts as a record of what was run, not as a reusable harness.

## How much headroom is left

Estimating the noise content as the per-pixel temporal standard deviation (median 9.8–34.4 ADU
depending on session) gives a Gaussian-entropy figure of 5.3–7.1 bits/px, against JPEG-LS's actual
5.8–6.8. That estimate is a heuristic, not a bound — it counts real calcium signal as if it were
noise and ignores spatial correlation entirely, and in one session JPEG-LS beats it. So the honest
statement is the weaker one: **JPEG-LS is close to the best of the methods tested here, on the data
sampled, and no additional transform tried has shown a worthwhile gain.** That these recordings are
largely shot-noise-limited makes a large remaining win unlikely, but that has not been established.
