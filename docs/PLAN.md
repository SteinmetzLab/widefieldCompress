# In-place lossless compression of widefield raw-frame tars

Plan grounded in a full inventory of both lab servers and codec benchmarks run on real frames from
three sessions across both on-disk flavours. Nothing has been written to either server.

**Requirement (confirmed):** "same frames, same pixels" — byte-identical regeneration of the
original `.tar` is *not* required, and nothing reads these files directly, so the compressed form
may be slow or obscure to reconstruct.

> **In the event we get byte-identical anyway, for free.** The rebuild metadata (all tar headers,
> the TIFF shell, trailing padding) costs 0.01 % of the output, so `wfcompress.py` keeps it and the
> restored tar hashes equal to the original. Same-pixels remains the *requirement*; exactness is a
> bonus we didn't have to pay for, and it makes every verification a single hash comparison.

**Scope decisions (confirmed):**
- Nothing reads `widefield.tar` — they are backup only. No reader shim or migration needed.
- **`Z:` is out of scope for now.** Old and inactive; worth rescuing later but not the priority.
  All the Z:-staging machinery in §5 is deferred, not cancelled. **Target is `Y:` only:
  ~1,120 tars, 120.7 TB.**
- The bulk job will run **on the server over SSH**; the pilot ran from the Windows workstation.

---

## 0. Phase 0.5 pilot — done

`Y:\temp\wfCompressTest`, from `Y:\Subjects\FD_010\2026-02-23\3\widefield.tar` (3.01 GB,
4,752 frames, Basler TIFF). Source folder untouched; everything written to the test dir only.

| | |
|---|---|
| `widefield.ORIGINAL.tar` | 3.01 GB |
| `widefield.wfz` | 1.04 GB — **2.88×, 65.3 % saved** |
| `widefield.RESTORED.tar` | 3.01 GB, **sha256 identical to the original** |

Detected shift 4 (10-bit payload in bits 4–13) automatically; without that step this file
compresses only ~1.6×. Compress 0.7 min @ 68 MB/s *including* the per-frame verify decode;
decompress 0.5 min @ 91 MB/s. Cross-checked independently of my code: `tar tvf` listings match on
all 4,753 entries, and a frame extracted from each with GNU `tar` compares equal under `cmp`.

Not yet exercised: the `frame-N` flavour (headerless, geometry not in the file, not always square).

---

## 1. What is actually out there

Full recursive inventory (`tars_Y.txt`, `tars_Z.txt`, `tar_census.csv`):

| | files | size | free space on volume |
|---|---|---|---|
| `Y:` sahale | 1,126 | 120.7 TB | **146 TB free** |
| `Z:` steinmetzsuper1 | 478 | 49.6 TB | **156 GB free** ← effectively full |
| **total** | **1,604** | **170.3 TB** | |

**1,590 tars / 170.2 TB / ~272 M frames** are widefield and in scope. The rest: 7 zero-byte stubs
(just delete), 7 histology/lightsheet tars (~74 GB, exclude). Median tar 102 GB, p95 228 GB,
max 430 GB.

### Two on-disk flavours

| flavour | n | size | member looks like |
|---|---|---|---|
| `basler-tiff` | 1,489 | 162.0 TB | `1/Basler_acA2440-75um__23040354__20250712_182746728_0001.tiff` |
| `frame-N` | 101 | 8.2 TB | `1/frame-0`, or bare `frame-0` with no directory prefix |

- **`basler-tiff`** members are real TIFFs: uncompressed, big-endian, 16-bit, **one strip per image
  row**, so each frame carries ~4,626 B of header/strip tables — 0.73 %, ≈1.26 TB corpus-wide.
- **`frame-N`** members are headerless raw `uint16`. **They carry no geometry at all**, and frames
  are not always square — observed shapes include 560×560, 512×512, 540×540, 666×820, 609×616,
  551×548. Dimensions must come from a companion file (`blue/meanImage.npy` works) and the tool must
  refuse to guess when that is missing.

### Structural properties

- **Members are stored in lexicographic name order, not temporal order** —
  `frame-0, frame-1, frame-10, frame-100, frame-1000, frame-10000, frame-100000, …`
- **Member stride is constant**, so any frame is directly seekable by computed offset.
- **Blue and violet strictly alternate** (blue = even index), confirmed against
  `blueFrames.indexes.npy` / `violetFrames.indexes.npy`.
- **54 tars (5.1 TB) have no SVD output alongside** (`no_svd.csv`) — the tar is the only copy.
  The other 1,550 (165.1 TB) have `blue/svdSpatialComponents.npy` present.

---

## 2. The finding that matters most: the 16-bit word is mostly not used

Every sample is stored in a `uint16`, but **no session uses all 16 bits, and most waste bits at
*both* ends**. Sampling 3 frames from each of 90 random tars (`bitdepth_survey.csv`):

| flavour | low bits always zero | payload bits | n | sampled |
|---|---|---|---|---|
| basler-tiff | 4 | 12 | 54 | 5.34 TB |
| basler-tiff | 4 | 11 | 21 | 1.95 TB |
| basler-tiff | 4 | 10 | 7 | 0.80 TB |
| basler-tiff | 4 | 9 | 3 | 0.26 TB |
| frame-N | 0 | 11 | 5 | 0.49 TB |

**85 of 90 sampled tars are left-shifted by 4 bits** — the camera's 9–12 bit output sitting in bits
4..15 with the bottom nibble hard zero. On ZYE_0095 only 808 distinct values appear, spaced exactly
16 apart.

This wrecks JPEG-LS if you don't handle it, because every prediction residual comes out a multiple
of 16 and the codec spends bits coding four guaranteed-zero LSBs per sample:

| ZYE_0095 2025-07-12 | ratio |
|---|---|
| JPEG-LS, as stored | 1.63 |
| **JPEG-LS, after `>> 4`** | **2.76** |

**A per-session bit-shift normalisation is worth ~+69 % on affected sessions — roughly 40 TB across
the corpus.** It is a two-line transform (`OR` all samples, count trailing zeros, shift, record the
shift) and exactly invertible.

Two cautions: the shift is **not** a property of the flavour — AL_0048 is `basler-tiff` with
shift 0 — and payload width varies 9–12 bits. Both must be detected per session. Detecting from a
3-frame sample is not safe for production (one bright pixel could set a low bit); the tool should
derive the mask while streaming and let the round-trip verification catch any violation.

---

## 3. Codec results

Measured on real frames, round-trip verified lossless, after bit-shift normalisation. Throughput is
single-core.

| approach | ratio | enc MB/s | dec MB/s |
|---|---|---|---|
| zstd-3, raw bytes | 1.48 | 40 | 229 |
| TIFF deflate + predictor-2 | 1.69 | 45 | 143 |
| zstd-3 + byte-plane shuffle | 1.81 | 160 | — |
| JPEG-2000 reversible | 2.35 | 6.5 | 8.4 |
| JPEG-XL lossless, effort 3 | 2.36 | 10 | 16 |
| **JPEG-LS** | **2.37 – 2.76** | **30** | **39** |

Per session, with JPEG-LS:

| session | flavour | shift | payload | JPEG-LS | + mean-image subtracted |
|---|---|---|---|---|---|
| AL_0033 2025-03-05 | frame-N | 0 | 12 bit | 2.37 | 2.44 (−2.8 %) |
| ZYE_0095 2025-07-12 | basler | 4 | 10 bit | 2.76 | 2.81 (−1.7 %) |
| AL_0048 2026-07-01 | basler | 0 | 12 bit | 2.57 | 2.62 (−1.9 %) |

### On mean subtraction

It works, but it buys **~2 %**, and the reason is now pinned down:

- A **scalar** mean removal changes the output by **exactly 0.0 %** — JPEG-LS is shift-invariant, so
  the DC level costs nothing to begin with. "Subtly varying around a large mean" is already free.
- The gain therefore comes only from removing the *spatial structure* of the mean image (vessels,
  brain edge, illumination roll-off). JPEG-LS's MED predictor predicts each pixel from its left,
  upper and upper-left neighbours, so it already removes everything spatially smooth; only sharp
  repeated edges are left for mean subtraction to catch.
- A **block-local mean** (40-frame blocks, tracking bleaching/drift) adds only another ~0.4 %.

So the intuition is right, but the codec was already exploiting it. Recommend leaving it out of v1 —
~2 % is ~1.4 TB, against the cost of storing and validating a per-session mean image and a second
way for a session to fail. Easy to add behind a flag if the pilot has spare time.

### Also ruled out

- **Temporal delta hurts** (−4.5 %), before *and* after mean subtraction. Differencing two noisy
  frames doubles the noise variance and destroys the spatial smoothness the predictor relies on.
- **De-interleaving blue/violet** doesn't help (1.81 → 1.80 with zstd).
- **12-bit repacking as a pre-step** is a dead end: mechanically saves 25 % but entropy-codes worse.
- **JPEG-XL** ties JPEG-LS on ratio but is 3× slower to encode — days of extra wall clock for ~0 %.
- **JPEG-LS wrapped in TIFF via tifffile** only reached 1.96 vs 2.37 bare, probably a
  `bitspersample` difference. Irrelevant now that TIFF portability isn't required.

### How much headroom is left

Estimating the noise content as the per-pixel temporal standard deviation (median 9.8–34.4 ADU
depending on session) puts a Gaussian-entropy figure of 5.3–7.1 bits/px against JPEG-LS's actual
5.8–6.8. In two of three sessions JPEG-LS lands just above that figure; in the third it beats it.
That estimate is a heuristic rather than a bound — it counts real calcium signal as if it were
noise, and ignores spatial correlation entirely — but the fact that JPEG-LS sits within a few
percent of it in every session is good evidence **there is not another 20 % hiding anywhere.** These
recordings are shot-noise-limited and the noise is the incompressible part.

### Projected outcome (Y: only, 120.7 TB)

Four sessions measured so far: 2.37, 2.57, 2.76, 2.88. At a conservative planning figure of
**2.5×**: **~48 TB retained, ~72 TB reclaimed.** If the 2.88 of the pilot is nearer the norm,
~42 TB retained and ~79 TB reclaimed. Without the bit-shift step the same job lands near 74 TB
retained — i.e. that one transform is worth ~30 TB on Y: alone.

Z:, when it comes, adds 49.6 TB in and ~20 TB retained.

---

## 4. Recommended format

Per session, replacing `widefield.tar`:

```
widefield.jls.tar
  ├── frame-000000.jls …      bare JPEG-LS codestreams, in temporal order
  ├── _meta.json              geometry, dtype, bit shift, payload bits, n_frames,
  │                           channel interleave, source path + size,
  │                           SHA-256 of the concatenated original pixel payloads
  └── _index.npy              int64 (n_frames, 3): offset, length, CRC32
```

Rationale, given the relaxed requirement:

- **Frames are re-ordered into temporal order**, which the originals are not. No cost, and it makes
  every downstream reader simpler.
- **Compression destroys the constant stride** the current tars rely on for random access; the index
  restores O(1) seek to any frame.
- **Verification is proportionate.** One SHA-256 per session over the concatenated original pixel
  bytes (272 M per-frame SHA-256s would be 8.7 GB of hashes), plus a per-frame CRC32 in the index —
  1.1 MB per session — for cheap targeted re-checks later.
- Still a plain tar, so `tar tf` works and nothing exotic is needed to inspect it. A flat
  `header + codestreams + index` file would do just as well; the tar is for familiarity, not
  function.

Ship a ~200-line `wfarchive` reader with it: `open(path)` → `.n_frames / .shape / .frame(i)`,
plus `verify(path)`. Point it at `.tar` or `.jls.tar` transparently so callers never branch.

---

## 5. "In-place" — and the Z: drive problem

Never rewrite in place. Per session:

1. Stream the original; decode members; compute the session SHA-256 over raw pixel bytes.
2. Derive the bit-shift mask; encode each frame; **immediately decode it back in RAM and compare to
   the source pixels**. Costs CPU, no extra I/O. Any mismatch aborts, original untouched.
3. Write `widefield.jls.tar.tmp`.
4. **Re-read the finished file from the server** (so the page cache can't lie), decode everything,
   check CRC32s and the session SHA-256.
5. `rename` → `widefield.jls.tar` (atomic within a volume).
6. Only then delete `widefield.tar`, and write a JSON receipt: hashes, ratio, shift, tool version,
   host, timestamps, frame count.

**`Y:` is easy** — 146 TB free, output next to input, no staging. This is the whole job for now.

**`Z:` — deferred.** Kept here for when it comes back into scope. With 156 GB free you cannot write
even one median output beside its input,
and the largest Z: tar is 229 GB. Per Z: file: compress → write to scratch **on `Y:`** → verify
there → delete the Z: original (frees 100–229 GB) → move the verified file back to Z: → re-verify
after the move. Between the delete and the move the only copy is the verified compressed one on Y:.
Process Z: largest-first so headroom grows fastest.

**Still worth deciding first: does Z: content need to stay on Z: at all?** Y: has 146 TB free and Z:
is full. If those 478 sessions can live on Y:, the staging disappears and it becomes a plain move.

---

## 6. Phasing

**Phase 0 — inventory (done).** `tar_census.csv`, `no_svd.csv`, `bitdepth_survey.csv` here. Re-run
before starting; the census walked `Y:\Subjects` and `Z:\{Subjects_archive1,Subjects_OLD,tmp}` to
depth 5, so confirm no tars live elsewhere on Z:.

**Phase 1 — pilot, ~10 sessions, nothing deleted.** Both flavours, ≥4 geometries including a
non-square one, oldest and newest eras, at least one shift-0 and one shift-4 session, and at least
one `frame-N` session *without* a `meanImage.npy` to confirm it fails loudly rather than guessing
geometry. Confirm ratio spread, measure real end-to-end SMB throughput, prove the round trip.

**Phase 2 — bulk `Y:`, SVD-backed sessions only** (1,550 tars, 165.1 TB), largest-first, N workers
each owning one session end-to-end, checkpointed so an interrupted run resumes. Hold the delete step
behind a flag for the first ~50 and audit the receipts before enabling it.

**Phase 3 — `Z:`**, with the staging flow above.

**Phase 4 — the 54 no-SVD sessions (5.1 TB), last**, once the tooling has hundreds of verified
sessions behind it. Keep those originals until the SVD pipeline has run successfully off the
compressed versions.

---

## 7. Time

JPEG-LS at 30 MB/s encode + 39 MB/s verify-decode per core → ~17 MB/s/core for encode+verify.

- **16 cores**: ~270 MB/s → 170 TB ≈ **7–8 days of pure compute**.
- **Network**: 170 TB read + 68 TB written + 68 TB re-read to verify ≈ 306 TB. At a realistic
  ~700 MB/s SMB, ≈ **5 days**.

Overlapped and flat out, ~1.5 weeks; realistically **3–4 weeks** with normal lab contention. Two
things change that materially:

- **Run it on the server itself** if it has spare CPU — removes ~306 TB of network traffic and is by
  far the biggest single win available.
- **Throttle deliberately.** A job that saturates the share ruins everyone's day. Cap workers,
  consider nights and weekends.

---

## 7a. Geometry coverage on Y: (checked)

TIFF members carry their own geometry; headerless ones do not, and the only authoritative source is
`blue/meanImage.npy`.

| | archives | size |
|---|---|---|
| TIFF, geometry self-evident | 1,025 | 112.53 TB |
| headerless, geometry recoverable | 83 | 6.47 TB |
| **headerless, blocked — no `meanImage.npy`** | **18** | **1.68 TB** |

**All 18 blocked archives also have no SVD output**, so they are unprocessed sessions where the tar
is the only copy — the highest-risk category, and they need a human decision rather than a default.

Two things make them tractable when someone wants to deal with them:

- Every one has **exactly 627,200 B members**, identical to the 83 headerless archives whose
  geometry *is* known, and all 83 of those are 560×560. So 560×560 is near-certain.
- A wrong-but-same-size shape is **not a data-integrity risk**. Pixels are stored and returned in
  file order regardless of how they are shaped for the predictor, so the tar still rebuilds
  byte-for-byte; the cost is a worse ratio and a wrongly-shaped array out of `WfzReader.frame()`.
  (`tests/test_roundtrip.py::test_size_compatible_wrong_shape_still_restores_byte_identically`.)

So `--shape 560 560` is a defensible call for these, with bounded downside. It should still be an
explicit decision, not a default — which is why the tool refuses rather than guessing.

## 8. Next steps

Resolved: nothing reads the tars; Z: deferred; the bulk job runs on the server over SSH; the raw
data is kept (not deleted) even where SVD exists, deliberately, to be safe.

Remaining before a bulk run:

1. **Pilot the `frame-N` flavour** — headerless, geometry only from `blue/meanImage.npy`, and not
   always square. Include one session where that file is *missing*, to confirm the tool refuses to
   guess rather than writing a garbled output.
2. **Get it running on the server over SSH** — needs `imagecodecs`, `zstandard`, `tifffile`,
   `numpy` there, and a throughput measurement with no network in the path.
3. **Batch driver**: work queue over the Y: census, N sessions in parallel, checkpointed and
   resumable, per-session JSON receipts, a `--no-delete` default, and a throttle so it doesn't
   saturate the share during working hours.
4. **Decide the delete policy** — I'd keep originals for the first ~50 sessions, audit the receipts,
   then enable deletion for the rest.
5. **Re-run the census immediately before the bulk job**; the current one is a snapshot and new
   sessions land continuously.
