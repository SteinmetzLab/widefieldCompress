"""The two small files written next to each .wfz.

``<name>.wfz.README.md``    for a human who finds the file and has no idea what it is
``<name>.wfz.receipt.json`` machine-readable audit record; this is what you consult before
                            deciding it is safe to delete an original

The authoritative copy of all of this also lives inside the .wfz footer, which cannot become
separated from the data. These two exist purely so that neither discovery nor auditing requires
opening the container.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np

from .provenance import REPO_URL

README_TEMPLATE = """# {stem}.wfz — losslessly compressed camera frames

This replaces `{source_name}`, an uncompressed tar of {n_frames:,} camera frames.
It is **lossless**: the original archive can be rebuilt byte-for-byte.

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
wfcompress decompress {stem}.wfz {stem}.tar
```

That rebuilds the original tar. To confirm it is identical to what went in:

```bash
sha256sum {stem}.tar        # compare with tar_sha256 in {stem}.wfz.receipt.json
```

To read frames directly without rebuilding the tar:

```python
from wfcompress import WfzReader
with WfzReader("{stem}.wfz") as r:
    print(r.n_frames, r.shape)
    frame = r.frame(0)          # numpy array, exactly as acquired
```

## What was done to the pixels

Nothing lossy. The pixel values you get back are bit-identical to the originals.

The only transform is that always-zero low bits are stripped before encoding and restored on read.
This camera wrote {payload_bits}-bit samples left-shifted into a 16-bit word, so the bottom
{shift} bits of every sample were hard zero. Removing them is exactly invertible and roughly
doubles the compression ratio.

Full source, format specification and rationale: {repo}
"""


def write_readme(wfz_path: str | Path, meta: dict) -> Path:
    wfz_path = Path(wfz_path)
    stem = wfz_path.name[: -len(".wfz")] if wfz_path.name.endswith(".wfz") else wfz_path.stem
    prov = meta.get("provenance", {})
    commit = prov.get("git_commit") or "unknown"
    shift = meta.get("shift", 0)
    text = README_TEMPLATE.format(
        stem=stem,
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
    )
    out = wfz_path.with_name(wfz_path.name + ".README.md")
    out.write_text(text, encoding="utf-8")
    return out


def write_preview_frame(wfz_path: str | Path, n_probe: int = 7) -> Path | None:
    """Write one representative frame beside the .wfz as an ordinary TIFF.

    Two reasons this earns its ~0.6 MB. It lets anyone open a frame in Fiji, MATLAB or a browser
    with no tooling at all; and for archives of headerless frames it makes the frame geometry --
    which is *not* recoverable from the archive itself -- visible and checkable by eye.

    For TIFF archives the original member is reproduced byte-for-byte, so all the camera metadata
    comes with it. For headerless archives a minimal TIFF is synthesised and labelled as such.

    The frame is chosen as the brightest of a spread of candidates, never frame 0: recordings
    routinely begin before the illumination is on, and a blank preview would be worse than none.
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
        best_i, best_max = int(picks[0]), -1
        for i in picks:
            m = int(r.frame(int(i)).max())
            if m > best_max:
                best_i, best_max = int(i), m
        pixels = r.frame(best_i)
        meta = r.meta
        member_name = r.member_name(best_i)

    out = wfz_path.with_name(wfz_path.name + ".frame.tif")
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
    return out


def _shell_for(wfz_path: Path, i: int) -> bytes:
    from . import container

    return container.read_footer(wfz_path).shell_for(i)


def write_receipt(wfz_path: str | Path, meta: dict, extra: dict | None = None) -> Path:
    wfz_path = Path(wfz_path)
    receipt = {k: v for k, v in meta.items() if k != "how_to_decompress"}
    receipt.update(extra or {})
    out = wfz_path.with_name(wfz_path.name + ".receipt.json")
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    return out
