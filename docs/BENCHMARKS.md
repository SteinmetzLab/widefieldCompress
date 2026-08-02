# Benchmarks

All measurements on real widefield frames from the Steinmetz Lab share, 16-bit, round-trip verified
lossless (`np.array_equal` on every frame). Throughput is single-core unless stated.
Scripts in [`../scripts/`](../scripts/).

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

**Subtracting the mean image: ~2 %.** The intuition — "the data is subtle variation around a large
mean" — is right, but the codec already exploits it. A *scalar* mean removal changes the output by
**exactly 0.0 %**: JPEG-LS is shift-invariant, so the DC level is free to begin with. The residual
2 % comes only from removing the mean image's *spatial* structure (vessels, brain edge,
illumination roll-off), and the MED predictor — which predicts each pixel from its left, upper and
upper-left neighbours — already removes anything spatially smooth. A block-local mean tracking
photobleaching adds another 0.4 %.

**Temporal differencing: −4.5 %, i.e. worse.** Differencing two noisy frames doubles the noise
variance, and the result has no spatial smoothness left for the predictor to use. True for
same-channel (step-2) deltas, naive step-1 deltas, and mean-subtraction-then-delta alike. Delta only
helps the weaker compressors (zstd 1.81 → 1.91).

**De-interleaving blue/violet: nothing** (zstd 1.81 → 1.80).

**12-bit repacking as a pre-step: worse.** Mechanically saves 25 % but entropy-codes badly
afterwards (zstd-3 on packed data: 1.43, vs 1.81 on byte-shuffled).

**JPEG-LS inside a TIFF: 1.96 vs 2.37 bare**, probably a `bitspersample` difference in how
tifffile drives the encoder.

## How much headroom is left

Estimating the noise content as the per-pixel temporal standard deviation (median 9.8–34.4 ADU
depending on session) gives a Gaussian-entropy figure of 5.3–7.1 bits/px, against JPEG-LS's actual
5.8–6.8. That estimate is a heuristic, not a bound — it counts real calcium signal as if it were
noise and ignores spatial correlation entirely, and in one session JPEG-LS beats it. But landing
within a few percent of it in every session is good evidence that there is no large win left. These
recordings are shot-noise-limited, and the noise is the incompressible part.
