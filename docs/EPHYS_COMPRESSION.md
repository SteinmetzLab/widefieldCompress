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

## Measured on your data

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

## Census

See `data/ephys_census_Y.csv` and `scripts/run_ephys_census.py`. The walk is slow — the share is
576 subjects and 8,725 date folders, and traversal is pure SMB latency, made worse by the
compression job holding the link. Results are recorded in the census summary when it completes.
