"""The two small files written next to each .wfz.

``<name>.wfz.README.md``    for a human who finds the file and has no idea what it is
``<name>.wfz.receipt.json`` machine-readable audit record; this is what you consult before
                            deciding it is safe to delete an original

The authoritative copy of all of this also lives inside the .wfz footer, which cannot become
separated from the data. These two exist purely so that neither discovery nor auditing requires
opening the container.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import filelog
from .provenance import REPO_URL

README_TEMPLATE = """# {stem}.wfz — losslessly compressed camera frames

This replaces `{source_name}`, an uncompressed tar of {n_frames:,} camera frames.
It is **lossless**. {verified_line}

| | |
|---|---|
| original | {source_bytes:,} bytes |
| compressed | {output_bytes:,} bytes |
| ratio | **{ratio:.2f}x** ({saved:.1f}% saved) |
| frames | {n_frames:,} of {shape} {dtype} |
| codec | JPEG-LS, lossless mode (near=0){shift_note} |
| written | {written} by wfcompress {version} (`{commit}`) |

## Getting the data back

```bash
pip install git+{repo}@{commit_ref}
```

**If you want the frames** — a folder of the original image files, exactly as `tar -xf` would have
given you, without ever materialising the tar:

```bash
wfcompress extract {stem}.wfz ./frames/
```

**If you want one flat array** — headerless binary, `{rows} x {cols} x {n_frames}` uint16,
frames in acquisition order, the same shape of file as a SpikeGLX `.ap.bin`. Usually the fastest
thing to get into analysis code, and memory-mappable:

```bash
wfcompress extract {stem}.wfz {stem}.bin --bin
```
```python
import numpy as np
mov = np.memmap("{stem}.bin", dtype="{bin_dtype}", mode="r").reshape(-1, {rows}, {cols})
```
Geometry is repeated in `{stem}.bin.json`, since the binary itself carries no header.
Add `--frames FIRST LAST` to pull out only part of a recording.{endian_note}

**If you want the original archive back**, byte for byte:

```bash
wfcompress decompress {stem}.wfz {stem}.tar
sha256sum {stem}.tar        # compare with tar_sha256 in {stem}.wfz.receipt.json
```

**If you want a few frames in Python**, with no intermediate files at all:

```python
from wfcompress import WfzReader
with WfzReader("{stem}.wfz") as r:
    print(r.n_frames, r.shape)
    frame = r.frame(0)          # numpy array, exactly as acquired
```

> Note on ordering: this tar was written in lexicographic member-name order
> (`frame-0, frame-1, frame-10, frame-100`), so position in the archive is **not** position in the
> recording. `extract --bin` and `WfzReader.frame(i)` both undo that; `extract` to a folder keeps
> the original filenames, which carry the frame number.

## What was done to the pixels

Nothing lossy. The pixel values you get back are bit-identical to the originals.

The only transform is that always-zero low bits are stripped before encoding and restored on read.
This camera wrote {payload_bits}-bit samples left-shifted into a 16-bit word, so the bottom
{shift} bits of every sample were hard zero. Removing them is exactly invertible and roughly
doubles the compression ratio.

Full source, format specification and rationale: {repo}
"""


_VERIFIED = (
    "The rebuild has been verified byte-for-byte: the reconstructed archive was hashed end to "
    "end and matched the original."
)

_UNVERIFIED = (
    "Every frame round-tripped and every member reassembled to its original bytes during "
    "compression, but the whole rebuilt archive has **not** been hashed end to end. "
    "Run `wfcompress check` on this file to establish byte-identity."
)


def write_readme(wfz_path: str | Path, meta: dict, file_log=None) -> Path:
    wfz_path = Path(wfz_path)
    stem = wfz_path.name[: -len(".wfz")] if wfz_path.name.endswith(".wfz") else wfz_path.stem
    prov = meta.get("provenance", {})
    commit = prov.get("git_commit") or "unknown"
    shift = meta.get("shift", 0)
    rows, cols = (list(meta.get("shape", (0, 0))) + [0, 0])[:2]
    # `extract --bin` writes little-endian whatever the archive holds, because part of this corpus
    # is big-endian TIFF and part is little-endian raw. The snippet has to say what comes out, not
    # what went in, or it hands the reader byte-swapped values.
    dtype = str(meta.get("dtype", "<u2"))
    bin_dtype = "<" + dtype[1:] if dtype[:1] in "<>=|" else dtype
    text = README_TEMPLATE.format(
        stem=stem,
        rows=rows,
        cols=cols,
        bin_dtype=bin_dtype,
        endian_note=(
            f"\n\nThe frames are stored {dtype} (big-endian) inside the archive; `--bin` writes "
            f"{bin_dtype} so ordinary readers work. `--byteorder source` keeps the original bytes."
            if dtype != bin_dtype
            else ""
        ),
        source_name=meta.get("source_name", "the original tar"),
        source_bytes=meta.get("source_bytes", 0),
        output_bytes=meta.get("output_bytes", 0),
        ratio=meta.get("ratio", 0.0),
        saved=100 * (1 - meta.get("output_bytes", 0) / max(meta.get("source_bytes", 1), 1)),
        n_frames=meta.get("n_frames", 0),
        shape=tuple(meta.get("shape", ())),
        dtype=meta.get("dtype", "?"),
        shift_note=f", after a {shift}-bit right shift" if shift else "",
        written=prov.get("written_utc", "?"),
        version=prov.get("version", "?"),
        commit=commit,
        commit_ref=commit.split("+")[0] if commit != "unknown" else "main",
        repo=REPO_URL,
        payload_bits=meta.get("payload_bits", "?"),
        shift=shift,
        verified_line=_VERIFIED if meta.get("byte_identical_verified") else _UNVERIFIED,
    )
    out = wfz_path.with_name(wfz_path.name + ".README.md")
    existed = out.exists()
    out.write_text(text, encoding="utf-8")
    filelog.record_write(file_log, out, existed)
    return out


def write_preview_frame(wfz_path: str | Path, n_probe: int = 7, file_log=None) -> Path | None:
    """Write one representative frame beside the .wfz as an ordinary TIFF.

    Two reasons this earns its ~0.6 MB. It lets anyone open a frame in Fiji, MATLAB or a browser
    with no tooling at all; and for archives of headerless frames it makes the frame geometry --
    which is *not* recoverable from the archive itself -- visible and checkable by eye.

    For TIFF archives the original member is reproduced byte-for-byte, so all the camera metadata
    comes with it. For headerless archives a minimal TIFF is synthesised and labelled as such.

    The frame is chosen by a robust high percentile over a spread of candidates, never frame 0:
    recordings routinely begin before the illumination is on, and a blank preview would be worse
    than none. A percentile rather than the maximum, so one hot pixel cannot decide the choice.
    """
    import tifffile

    from .frames import FrameLayout, join
    from .reader import WfzReader

    wfz_path = Path(wfz_path)
    with WfzReader(wfz_path) as r:
        n = r.n_frames
        if n == 0:
            return None
        picks = np.unique(np.linspace(0, n - 1, min(n_probe, n)).astype(int))
        # a robust high percentile, not the maximum: a single hot pixel or a saturated
        # speck would otherwise decide which frame represents the session
        best_i, best_score = int(picks[0]), -1.0
        for i in picks:
            score = float(np.percentile(r.frame(int(i)), 99.0))
            if score > best_score:
                best_i, best_score = int(i), score
        pixels = r.frame(best_i)
        meta = r.meta
        member_name = r.member_name(best_i)

    out = wfz_path.with_name(wfz_path.name + ".frame.tif")
    existed = out.exists()
    if meta["is_tiff"]:
        # rebuild the original member exactly, shell and all
        footer_shell = _shell_for(wfz_path, best_i)
        layout = FrameLayout(
            shape=tuple(meta["shape"]),
            dtype=np.dtype(meta["dtype"]),
            px_start=meta["px_start"],
            px_len=meta["px_len"],
            is_tiff=True,
        )
        out.write_bytes(join(pixels, footer_shell, layout))
    else:
        tifffile.imwrite(
            out,
            np.ascontiguousarray(pixels),
            photometric="minisblack",
            description=(
                f"Synthesised preview of frame {best_i} from {wfz_path.name} "
                f"({member_name}). The source archive stores headerless raw frames and carries no "
                f"geometry; {meta['shape'][0]}x{meta['shape'][1]} was supplied at compression "
                f"time. If this image does not look like what you expect, that shape was wrong. "
                f"See {REPO_URL}"
            ),
        )
    filelog.record_write(file_log, out, existed)
    return out


def _shell_for(wfz_path: Path, i: int) -> bytes:
    from . import container

    return container.read_footer(wfz_path).shell_for(i)


def write_receipt(
    wfz_path: str | Path, meta: dict, extra: dict | None = None, file_log=None
) -> Path:
    wfz_path = Path(wfz_path)
    receipt = {k: v for k, v in meta.items() if k != "how_to_decompress"}
    receipt.update(extra or {})
    out = wfz_path.with_name(wfz_path.name + ".receipt.json")
    existed = out.exists()
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    filelog.record_write(file_log, out, existed)
    return out
