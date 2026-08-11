# Compressing the raw ephys: is `mtscomp` the right tool?

**Short answer: yes, use it.** It is purpose-built for exactly this data, lossless, self-verifying,
and already the format the rest of your ecosystem reads. Measured on one of your own recordings it
gives **×2.56**.

## What it does

[mtscomp](https://github.com/int-brain-lab/mtscomp) is the International Brain Laboratory's
lossless compressor for flat electrophysiology binaries. Per chunk (1 s by default):

1. take the **time difference** along the sample axis — successive samples on a channel are highly
   correlated, so the residual is small and near-zero-centred;
2. flatten in Fortran order, so a chunk is laid out channel-major (worth a few % over C order);
3. **zlib** the result;
4. write the chunk to `.cbin` and its byte offset to a JSON `.ch` index.

The chunk index is the point of the design: any time range can be read without decompressing what
comes before it, which is what makes a `.cbin` a drop-in replacement for a `.bin` in a sorter.

Spatial differencing across channels exists but is **off** by default — their benchmarks found it
didn't help, which matches the physics (neighbouring channels share reference noise but not much
else at 30 kHz).

## Measured on sahale itself

The box turns out to be the right place to run this: two Xeon Silver 4210R, **40 threads**, 273 GB
RAM, numpy 1.22.4 already installed. No pip, but mtscomp is one pure-Python file and tqdm has no
compiled parts, so 0.21 MB staged on the share is the whole install.

4 GB prefix of `AL_0039/2025-09-30/6`, 385 ch @ 30 kHz, reading straight off the pool:

| | |
|---|---|
| **local pool read** | **423 MB/s** (vs ~20 MB/s over SMB from the workstation while the widefield campaign runs) |
| 8 threads | 29.7 MB/s, ×2.56 |
| 16 threads | 39.0 MB/s, ×2.56 |
| 32 threads | 46.6 MB/s, ×2.56 |

**Two things to read off that.** The ratio is identical to the workstation measurement — ×2.56 —
so it is a property of the data, not the machine. And **thread scaling is poor**: four times the
threads bought 1.57×.

The reason is visible in the run output. mtscomp's verify pass took **28, 30 and 29 seconds** in
the three runs — flat, regardless of thread count, so it is effectively serial. At 32 threads that
is a third of the wall clock not scaling at all:

| | 4 GB in | rate |
|---|---|---|
| compress, 32 threads | 56 s | 71 MB/s |
| verify (serial) | 29 s | 138 MB/s |
| combined | 85.8 s | **46.6 MB/s** |

At 46.6 MB/s the 94.79 TB corpus is **23.5 days**. Better than the 55 days it would take over SMB
from the workstation, but well short of what the hardware should give.

**The fix is almost certainly whole processes rather than more threads** — the same lesson the
widefield side learned, where `--jobs` beat `--threads` by about 2× for the same core count. Each
process gets its own serial verify, so they overlap instead of queueing.
`scripts/sahale_mtscomp_parallel.py` measures this; it is staged on the share alongside the rest.

## Measured on the workstation

`AL_0039/2025-09-30/6`, Neuropixels 1.0, 385 channels @ 30 kHz, first 4 GB (173 s of recording):

| | |
|---|---|
| 4.00 GB → **1.56 GB** | **×2.56, 61% saved** |
| `.ch` index | 0.01 MB — negligible |
| decompress → SHA-256 vs original | **IDENTICAL** |
| compress | 15 MB/s, decompress 50 MB/s |

Two caveats on those speeds: the machine had all 16 cores busy with the widefield campaign, and
the compress figure includes mtscomp's own verify pass. The project reports 88 MB/s compress and
22 MB/s decompress on an idle machine; treat mine as a floor, not a measurement of the tool.

×2.56 is a little under the ~3× in their README. That is expected — ratio depends on the noise
floor and on how much of the dynamic range the recording actually uses.

## Why it is the right choice here

- **Lossless, and it proves it.** `check_after_compress=True` by default: the compressed file is
  decompressed and compared with the original before the run is called a success. Keep that on.
- **It is already your format.** IBL, ONE and SpikeInterface all read `.cbin` natively, and this
  lab already has `.cbin` files from IBL pipelines. Anything else would need a converter forever.
- **Trivial dependencies.** Pure Python, numpy, zlib, tqdm. No wheels to build — which is what
  killed the idea of running our widefield compressor on the TrueNAS box.
- **Random access.** Sorting a `.cbin` in place is normal practice; you are not committing to
  decompressing before every analysis.

## Things to know before committing

- **The project is dormant.** Latest release is 1.0.2, May 2021. That is five years old. Against
  that: the format is frozen at version 1.0, it is ~1,000 lines of Python, and it imported and ran
  correctly on the current interpreter here. The risk is "nobody fixes a bug", not "it will stop
  working".
- **zlib, not zstd.** zstd would be faster at a similar ratio. It would also make the output
  unreadable by every tool in the IBL stack, which is a bad trade for a few percent.
- **The integrity check uses `np.allclose(..., atol=1e-16)`.** For int16 that is effectively exact
  — any real difference is at least 1, and the implied tolerance is under 0.33 — but it is an odd
  way to compare integers, and it would *not* be exact for a float dtype. Only compress int16
  with it, which is all SpikeGLX writes anyway.
- **It needs `nSavedChans` and the sample rate**, which come from the `.meta`. Any `.bin` whose
  `.meta` is missing has to be handled by hand; the census counts those separately.
- **`.lf` compresses differently from `.ap`.** The LFP band is smoother and should do better; the
  measurement above is `.ap` only. Worth a second measurement before sizing the LF part precisely.

## Recommendation

Use mtscomp, defaults, `check_after_compress` on. Compress `.ap` and `.lf` in place, verify, and
keep the `.cbin`/`.ch` pair. Do **not** write our own — the format compatibility is worth far more
than any ratio we would gain, and mtscomp already does the one thing that matters (prove the round
trip) properly.

The one piece worth building is the same batch harness the widefield campaign has: resumable,
logging every file it touches to `fileEditLog.csv`, refusing to delete a raw file until the
compressed one has been verified. `wfcompress.lab.batch` is that harness and would need modest
adaptation rather than a rewrite.

## Census of Y: — 95 TB of raw ephys, about 58–61 TB reclaimable

`data/ephys_census_Y.csv`, from `scripts/run_ephys_census.py`. **1,938 raw `.bin` files,
95.02 TB**, across 184 subjects. Zero unreadable directories.

| band | files | size | |
|---|---|---|---|
| `.ap` | 1,524 | **92.88 TB** | 97.8% |
| `.lf` | 401 | 2.13 TB | 2.2% |
| `.nidq` | 13 | 0.004 TB | — |

**Only 12 files already have a `.cbin` beside them** — 0.23 TB raw. So essentially none of this
has been compressed. Those 12 give a useful second data point: their `.cbin` files imply an
**observed ×2.82**, better than the ×2.56 measured directly on a fresh file.

| at | kept | **reclaimed** |
|---|---|---|
| ×2.56 (measured here) | 37.03 TB | **57.76 TB** |
| ×2.82 (observed on the 12 already done) | 33.66 TB | **61.13 TB** |

Plus 0.23 TB from deleting the raw files that already have a verified `.cbin`. Call it
**~58–61 TB**, i.e. comparable to the whole widefield campaign (68 TB) — and from 1,938 files
rather than 1,120 archives, so the per-file overhead is lower.

Concentration is mild: the top ten subjects hold ~21 TB between them, led by `IBL` at 2.85 TB and
`JRS_0040` at 2.68 TB. The largest single file is 452.7 GB (`JRS_0059/2026-02-13`).

**26 files (224 GB) have no `.meta`** and would need their channel count and sample rate supplied
by hand, or to be skipped.

> **Depth matters — the first attempt undercounted by 22%.** Walking to depth 7 found 1,592 files
> and 92.16 TB; going to depth 14 found **1,938 files and 95.02 TB**. Almost all of the difference
> appeared at depth 8 (340 files). Nothing new turned up past depth 9, and the walk still had 64
> unexplored directories at depth 15, so a small residual gap remains — but no `.bin` has appeared
> that deep, so it is very unlikely to matter. The depth-7 result is kept as
> `data/ephys_census_Y_depth7.csv` for comparison.

### On the walk itself

Two things to know if this is re-run. First, use `os.scandir`, not `Path.iterdir()` plus
`.is_dir()` — the latter costs a separate stat per entry, which over SMB took the 576-entry root
from 5 seconds to over four minutes. Second, the cost is concentrated in a handful of session
folders holding hundreds of thousands of loose files; those take minutes each to list no matter
how many threads are used, which is what makes the walk look hung when it is only slow.
