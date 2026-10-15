# wfcompress

> **Picking this up cold? Read [docs/HANDOFF.md](docs/HANDOFF.md) first** — current state of both
> compression campaigns, what is running, and the open decisions.

Lossless compression of tar archives that hold one uncompressed camera frame per member.

Writing every frame as its own uncompressed TIFF (or headerless raw block) into a tar is a common
way to get data off an acquisition machine quickly — the write is cheap and the next session can
start. It is also extremely wasteful to keep. This turns those archives into a form that is
**typically 2.2–2.9× smaller and rebuilds the original byte-for-byte**.

Built for widefield calcium imaging in the [Steinmetz Lab](https://www.steinmetzlab.net), but the
core knows nothing about widefield, or about any particular server.

```bash
pip install git+https://github.com/SteinmetzLab/widefieldCompress

wfcompress compress   widefield.tar widefield.wfz
wfcompress check      widefield.wfz                  # proves byte-identity, writes nothing
wfcompress decompress widefield.wfz restored.tar
wfcompress verify     widefield.tar restored.tar     # -> IDENTICAL
```

## Getting data out

`decompress` rebuilds the archive, which is what you want to prove nothing was lost. It is not
what you want in order to *use* the data — you get a 200 GB tar you then have to untar. `extract`
skips that step:

```bash
wfcompress extract widefield.wfz ./frames/            # the original TIFFs, as tar -xf would give
wfcompress extract widefield.wfz wf.bin --bin         # one flat headerless uint16 binary
wfcompress extract widefield.wfz wf.bin --bin --frames 0 1000
```

`--bin` writes exactly `rows * cols * n_frames * 2` bytes, frames concatenated in **acquisition
order** — the same shape of file as a SpikeGLX `.ap.bin`, and the fastest thing to get into
analysis code:

```python
import numpy as np
mov = np.memmap("wf.bin", dtype="<u2", mode="r").reshape(-1, 560, 560)
```

Geometry is repeated in `wf.bin.json`, since the binary carries no header of its own.

Two things `--bin` normalises rather than transcribes, both of which are wrong the other way:

- **Frame order.** These tars are written in lexicographic member-name order (`frame-0, frame-1,
  frame-10, frame-100`), so archive position is *not* recording position. The permutation is stored
  at compression time and applied here. `--order storage` opts out.
- **Byte order.** Part of this corpus is big-endian TIFF and part is little-endian headerless raw,
  so copying the source bytes would make two identical-looking sessions need different readers.
  Output is little-endian by default; `--byteorder source` opts out.

Or read frames directly, with no intermediate file at all:

```python
from wfcompress import WfzReader

with WfzReader("widefield.wfz") as r:
    print(r.n_frames, r.shape)
    frame = r.frame(2000)        # numpy array, exactly as acquired, acquisition order
```

## What it does

1. **Splits each member** into its pixel block and its "shell" — the TIFF header and strip tables
   around it. The shell is usually identical for every frame, so one copy is stored.
2. **Strips always-zero low bits.** Scientific cameras routinely write 9–12 bit samples
   left-shifted into a 16-bit word. Those hard-zero LSBs are expensive to leave in: on real
   widefield data, handling this is the difference between **1.63× and 2.76×**. The shift is
   detected per archive, recorded, and undone on read.
3. **Encodes each frame with JPEG-LS** in lossless mode (`near=0`).
4. **Verifies while writing.** Every frame is decoded again immediately after encoding and
   compared with the source, and each member must reassemble to its original bytes. Any mismatch
   aborts before anything is committed.
5. **Records the source archive's SHA-256** during the sequential read, so `wfcompress check`
   can later prove byte-identity by streaming the reconstruction through a hash rather than
   writing a restored archive back out. That is 1.9x the source bytes in I/O rather than 4.9x --
   on a 100 TB corpus, the difference between weeks and months.

### Why JPEG-LS

Measured on real 16-bit widefield frames, after bit-shift normalisation:

| codec | ratio | enc MB/s | dec MB/s |
|---|---|---|---|
| zstd-3, raw bytes | 1.48 | 40 | 229 |
| TIFF deflate + predictor-2 | 1.69 | 45 | 143 |
| zstd-3 + byte-plane shuffle | 1.81 | 160 | — |
| JPEG-2000 reversible | 2.35 | 6.5 | 8.4 |
| JPEG-XL lossless, effort 3 | 2.36 | 10 | 16 |
| **JPEG-LS** | **2.37 – 2.88** | **30** | **39** |

JPEG-XL ties on ratio and is 3× slower to encode. Things that sound promising and are not:
**temporal differencing hurts** (−4.5 %; it doubles the noise variance and destroys the spatial
smoothness the predictor relies on), **subtracting the mean image gains ~2 %** (JPEG-LS is exactly
shift-invariant, so only the mean's *spatial structure* is worth anything, and the MED predictor
already removes most of that), and **de-interleaving channels does nothing**. See
[docs/BENCHMARKS.md](docs/BENCHMARKS.md).

These recordings are shot-noise-limited, and per-pixel temporal standard deviation puts the
practical floor within a few percent of what JPEG-LS already achieves. There is no large win left.

## The `.wfz` container

```
magic  b"WFZ1\0\0\0\0"          8 bytes
uint64 footer_offset            8 bytes, little-endian
<JPEG-LS codestreams, concatenated, in temporal order>
<footer: a zip archive>
```

The footer is an ordinary zip, readable with standard tools, holding `meta.json` (geometry, bit
shift, frame count, provenance, pixel SHA-256), `index.npy` (offset/length/CRC32 per frame),
`tarheaders.bin.zst`, `shells.bin.zst` and `trailer.bin`.

Compression destroys the constant member stride the original archives had, so the index is what
restores O(1) random access. The rebuild metadata costs **~0.01 % of the output**, which is why
byte-identical restore is worth keeping rather than settling for same-pixels.

Tar sources are written as format version 2. Version 3 is written only for wfAcq sessions (below):
it has no tar entries, an identity `order.npy`, and the small session files under `session/`. A
reader that predates version 3 refuses those files with an upgrade message, so every environment
that reads `.wfz` files (DataBrowser included) needs this version before the first one appears.

## wfAcq sessions

[wfAcq](https://github.com/SteinmetzLab/wfAcq) writes `wf.bin` (raw `uint16`, little-endian, frames x
height x width) plus a few small files, specified in wfAcq's `FORMAT.md`. A cleanly closed session
folder compresses to `widefield.wfz` beside it, the name DataBrowser looks for:

```bash
wfcompress compress path/to/session/            # compress, then verify the rebuild, then receipt
wfcompress check    path/to/session/widefield.wfz
wfcompress restore  path/to/session/widefield.wfz ./restored/       # wf.bin + every small file
wfcompress restore  path/to/session/widefield.wfz path/to/session/ --bin-only
```

- **Only sessions with `closed_cleanly: true` are accepted.** A session that did not finish
  closing can leave a partial frame or unrecorded frames in `wf.bin`, so it is refused for a person
  to look at. So are sessions whose files are newer than an hour (`--min-age-s`), whose `wf.bin`
  size disagrees with `bin_bytes`, or whose frames tables disagree with each other.
- Geometry comes from `wf_meta.json`, never from the file size. Values are stored unshifted.
- The small files are stored inside the `.wfz` as well as left in place, so the frames cannot be
  separated from their counters, timestamps and labels.
- `check` proves the `.wfz` holds exactly the bytes read from `wf.bin`. It does not prove that the
  copy that was read was complete. Deleting `wf.bin` should also re-hash it against
  `source_bin_sha256` in the receipt. Nothing here deletes anything.
- **A run stopped while it verifies can be finished.** The `.wfz` is renamed into place only once
  compression has finished, so a stop during the verify leaves a complete `.wfz` with no receipt.
  `wfacq.finish_session(session)` verifies it and writes the README, preview and receipt as
  `convert_session` would, without compressing again, if `wf.bin` and the small files are
  unchanged since they were read. Its receipt says `finished_after_stop: true`.
- The `.wfz`, the restored files and the receipt never replace anything: each refuses an existing
  file, including one that appears while a run is in progress. The `.wfz.README.md` and
  `.wfz.frame.tif` beside it only describe the `.wfz`, and are rewritten if present.

Measured on 42,000 real 560 x 560 frames (10 minutes at 70 Hz, 26.3 GB, from a local SATA SSD) on
a Ryzen 7 3700X at below-normal priority, compress plus the streaming verify:

| threads | compress | verify | per hour of recording | ratio |
|---|---|---|---|---|
| 4 | 145 MB/s | 269 MB/s | 28 min | 2.62 |
| 8 | 224 MB/s | 402 MB/s | 18 min | 2.62 |

## Layout

| | |
|---|---|
| `wfcompress` | the reusable core. No server paths, no site assumptions. |
| `wfcompress.lab` | Steinmetz-lab inventory and batch driver. Imports the core; never the reverse. |

`tests/test_core_is_standalone.py` enforces that boundary, and fails on any UNC path, drive letter
or `sys.path` manipulation appearing in the core.

## Requirements

Python ≥ 3.10, `numpy`, `imagecodecs`, `tifffile`, `zstandard`.

## Limitations

- Members must be single-page **uncompressed** 16-bit TIFFs with contiguous strips, or headerless
  raw 16-bit frames. Anything else raises rather than guessing.
- Headerless archives carry no geometry, so `--shape ROWS COLS` is required for them. Frames are
  not always square; a wrong shape produces a garbled but plausible image, so a mismatch between
  the supplied shape and the member size is a hard error.
- Single archive or session per invocation. Use `wfcompress.lab.batch` for bulk tar runs; there is
  no batch driver for wfAcq sessions yet.

## Licence

MIT.
