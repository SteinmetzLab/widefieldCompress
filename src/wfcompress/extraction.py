"""Get usable data out of a ``.wfz`` in one step.

:func:`decompress` rebuilds the original tar, which is the right thing when the goal is to prove
byte-identity or to restore an archive. It is the wrong thing when the goal is to *use* the data:
you end up with a 200 GB tar you then have to untar, doubling the space and the wait.

This module skips the tar. Two destinations:

``files``
    The original per-frame files, written straight into a directory - the folder of TIFFs you
    would have got from ``tar -xf``. Each file is byte-identical to the member inside the archive,
    original name, original modification time.

``bin``
    One headerless flat binary of ``rows * cols * n_frames * itemsize`` bytes, frames concatenated
    in **acquisition order**, matching the convention used for SpikeGLX ``.ap.bin`` files. Far
    faster to read into analysis code than several hundred thousand small TIFFs, and mmap-able.

Two things about the ``bin`` output are choices rather than transcription, and both are wrong by
default if made the other way:

*Frame order.* These archives are written in lexicographic member-name order (``frame-0, frame-1,
frame-10, frame-100``), so position in the archive is *not* position in the recording. The
container records the permutation and ``bin`` output applies it.

*Byte order.* Part of this corpus is TIFFs written big-endian (``MM``) and part is headerless
little-endian raw, so transcribing the source bytes would hand back files that need different
readers for sessions that look identical. ``bin`` output is little-endian by default - same pixel
values, the representation every default reader assumes. ``byteorder="source"`` keeps the archive's
own bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import zlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter

import imagecodecs
import numpy as np

from . import container, filelog, tarwalk
from .codec import DEFAULT_BATCH, DEFAULT_THREADS, LosslessCheckFailed

FORMATS = ("files", "bin")


class UnsafeMemberName(ValueError):
    """An archive member name would write outside the output directory.

    Tar member names come from the archive, not from the caller, so ``../../etc/passwd`` or an
    absolute path has to be refused rather than followed. These are lab archives written by our own
    acquisition machines, but extraction is the one operation where a name becomes a filesystem
    path, so it is checked anyway.
    """


def _safe_join(root: Path, name: str) -> Path:
    """``root / name``, refusing anything that escapes ``root``."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        raise UnsafeMemberName(f"member name is empty: {name!r}")
    if ".." in parts or name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise UnsafeMemberName(f"member name escapes the output directory: {name!r}")
    out = root.joinpath(*parts)
    if root.resolve() not in out.resolve().parents:
        raise UnsafeMemberName(f"member name escapes the output directory: {name!r}")
    return out


def _header_fields(h: bytes) -> tuple[str, int, int, bytes]:
    """(name, size, mtime, typeflag) from a raw 512-byte tar header."""
    name = h[:100].rstrip(b"\0").decode("utf-8", "surrogateescape")
    prefix = h[345:500].rstrip(b"\0").decode("utf-8", "surrogateescape")
    if prefix:
        name = f"{prefix}/{name}"
    size = tarwalk.parse_size(h[124:136])
    try:
        mtime = int(h[136:148].rstrip(b"\0 ").decode("ascii", "replace") or "0", 8)
    except ValueError:
        mtime = 0
    return name, size, mtime, h[156:157]


def _headers(footer) -> tuple[list[tuple[str, int, bytes]], list[tuple[str, bytes]]]:
    """Split the stored tar headers into (data members, zero-size entries).

    Data members come back in storage order, which is the order the payload index uses.
    """
    members, others = [], []
    blob = footer.tar_headers
    for k in range(0, len(blob), tarwalk.BLOCK):
        name, size, mtime, flag = _header_fields(blob[k : k + tarwalk.BLOCK])
        (members if size else others).append(
            (name, mtime, flag) if size else (name, flag)  # type: ignore[arg-type]
        )
    return members, others  # type: ignore[return-value]


def _selection(footer, n: int, first: int | None, last: int | None, order: str):
    """(storage_index, output_slot) pairs, sorted by storage index so reads run forward.

    ``first``/``last`` are a half-open range in whichever order was requested.
    """
    if order not in ("acquisition", "storage"):
        raise ValueError(f"order must be 'acquisition' or 'storage', got {order!r}")
    lo = 0 if first is None else max(0, first)
    hi = n if last is None else min(n, last)
    if lo >= hi:
        raise ValueError(f"empty frame range: [{lo}, {hi})")

    if order == "storage" or footer.order is None:
        pairs = [(i, i - lo) for i in range(lo, hi)]
    else:
        pairs = [(int(footer.order[t]), t - lo) for t in range(lo, hi)]
    pairs.sort()
    return pairs, hi - lo


def extract(
    src: str | Path,
    dst: str | Path,
    fmt: str = "files",
    first: int | None = None,
    last: int | None = None,
    order: str = "acquisition",
    byteorder: str = "little",
    threads: int = DEFAULT_THREADS,
    batch: int = DEFAULT_BATCH,
    progress: Callable[[int, int, float], None] | None = None,
    overwrite: bool = False,
    file_log: str | None = None,
) -> dict:
    """Decode a .wfz straight to per-frame files or to a flat binary.

    ``byteorder`` applies to ``fmt="bin"`` only; ``"files"`` output must reproduce the archive's
    bytes exactly, so it is ignored there.

    Returns a summary dict. ``pixels_verified`` is True only when the whole archive was extracted,
    in which case the pixel SHA-256 recorded at compression time is recomputed and checked. That
    hash is always taken over the archive's own bytes, so it still proves the pixels even when the
    binary was byte-swapped on the way out. Every frame's stored CRC-32 is checked either way, so a
    partial extraction is still guarded, just not against the whole-archive hash.
    """
    if fmt not in FORMATS:
        raise ValueError(f"fmt must be one of {FORMATS}, got {fmt!r}")
    if byteorder not in ("little", "source"):
        raise ValueError(f"byteorder must be 'little' or 'source', got {byteorder!r}")
    src, dst = Path(src), Path(dst)
    t0 = perf_counter()

    footer = container.read_footer(src)
    meta, index = footer.meta, footer.index
    n = int(meta["n_frames"])
    dtype = np.dtype(meta["dtype"])
    shape = tuple(meta["shape"])
    shift = meta["shift"]
    px_start, px_len = meta["px_start"], meta["px_len"]

    pairs, n_out = _selection(footer, n, first, last, order)
    whole = len(pairs) == n and [p[0] for p in pairs] == list(range(n))
    effective_order = "storage" if footer.order is None else order

    members, zero_entries = _headers(footer)
    if len(members) != n:
        raise LosslessCheckFailed(
            f"{src}: footer lists {len(members)} data members but meta says {n} frames"
        )

    # `body` is always the archive's own bytes, so the recorded pixel hash still applies; `payload`
    # is what actually gets written, which for a byte-swapped binary is not the same thing.
    out_dtype = np.dtype("<" + dtype.str[1:]) if byteorder == "little" else dtype
    swap = fmt == "bin" and out_dtype.str != dtype.str

    def decode(job):
        storage_i, slot, code = job
        if zlib.crc32(code) != np.uint32(index[storage_i, 2]):
            raise LosslessCheckFailed(f"frame {storage_i}: stored codestream fails its CRC")
        g = imagecodecs.jpegls_decode(code)
        arr = (g.astype(np.uint32) << shift).astype(dtype).reshape(shape)
        body = arr.tobytes()
        if len(body) != px_len:
            raise LosslessCheckFailed(
                f"frame {storage_i}: {len(body)} pixel bytes, expected {px_len}"
            )
        return storage_i, slot, body, (arr.astype(out_dtype).tobytes() if swap else body)

    pixel_hash = hashlib.sha256()
    written = 0

    if fmt == "bin":
        writer = _BinWriter(dst, n_out, px_len, overwrite=overwrite, file_log=file_log)
    else:
        writer = _FileWriter(  # type: ignore[assignment]
            dst, footer, members, zero_entries, px_start, overwrite=overwrite, file_log=file_log
        )

    with writer, open(src, "rb") as fin, ThreadPoolExecutor(threads) as pool:
        for start in range(0, len(pairs), batch):
            jobs = []
            for storage_i, slot in pairs[start : start + batch]:
                fin.seek(index[storage_i, 0])
                jobs.append((storage_i, slot, fin.read(index[storage_i, 1])))
            for storage_i, slot, body, payload in sorted(pool.map(decode, jobs),
                                                         key=lambda r: r[0]):
                pixel_hash.update(body)
                written += writer.write(storage_i, slot, payload)
            if progress:
                progress(min(start + batch, len(pairs)), len(pairs), perf_counter() - t0)

    verified = False
    if whole:
        if pixel_hash.hexdigest() != meta["pixels_sha256"]:
            raise LosslessCheckFailed(
                f"{src}: pixel SHA-256 does not match the value recorded at compression time"
            )
        verified = True

    summary = {
        "src": str(src),
        "dst": str(dst),
        "format": fmt,
        "n_frames": n_out,
        "n_frames_total": n,
        "shape": list(shape),
        "dtype": out_dtype.str if fmt == "bin" else dtype.str,
        "source_dtype": dtype.str,
        "byteswapped": swap,
        "order": effective_order,
        "order_requested": order,
        "temporal_order_known": footer.order is not None,
        "bytes_written": written,
        "pixels_verified": verified,
        "elapsed_s": perf_counter() - t0,
    }
    if fmt == "bin":
        summary["frame_bytes"] = px_len
        summary["sidecar"] = str(_write_bin_sidecar(dst, summary, meta, file_log))
    return summary


class _BinWriter:
    """Flat binary output. Frames arrive in storage order and are placed by seek.

    Reading the .wfz forward and seeking in the output is deliberately this way round: the source
    may be on a network share where sequential reads matter, while the destination is normally
    local disk where a 0.5-1 MB seek-and-write costs nothing.
    """

    def __init__(self, dst: Path, n_out: int, frame_bytes: int, overwrite: bool, file_log):
        self.dst, self.frame_bytes, self.file_log = dst, frame_bytes, file_log
        self.total = n_out * frame_bytes
        if dst.exists() and not overwrite:
            raise FileExistsError(f"{dst} exists; pass overwrite=True (--overwrite) to replace it")
        self.existed = dst.exists()
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(dst, "r+b" if self.existed else "w+b")
        self.fh.truncate(self.total)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fh.close()
        if exc[0] is None:
            filelog.record_write(self.file_log, self.dst, self.existed)

    def write(self, storage_i: int, slot: int, body: bytes) -> int:
        self.fh.seek(slot * self.frame_bytes)
        return self.fh.write(body)


class _FileWriter:
    """One file per member, byte-identical to what was inside the tar."""

    def __init__(self, root: Path, footer, members, zero_entries, px_start, overwrite, file_log):
        self.root, self.footer, self.members = root, footer, members
        self.px_start, self.file_log = px_start, file_log
        self.overwrite = overwrite
        self.n_written = self.bytes_written = 0
        root.mkdir(parents=True, exist_ok=True)
        # Zero-size entries are recreated first so the tree matches `tar -xf` even where a
        # directory holds no frames. One real archive ends with an empty `1/p0_g0/` tar'd in
        # alongside 426,324 frames, and dropping it would make the extraction quietly incomplete.
        for name, flag in zero_entries:
            if flag in (b"5", b"D"):
                _safe_join(root, name).mkdir(parents=True, exist_ok=True)
            elif flag in (b"0", b"\0", b""):
                p = _safe_join(root, name)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            # One row for the directory, not one per frame: an archive holds up to ~680,000 members
            # and a per-file row would bury the compression campaign's own audit trail.
            filelog.record(
                self.file_log,
                "create",
                self.root,
                size_bytes=self.bytes_written,
                note=f"extracted {self.n_written:,} frame files",
            )

    def write(self, storage_i: int, slot: int, body: bytes) -> int:
        name, mtime, _flag = self.members[storage_i]
        out = _safe_join(self.root, name)
        if out.exists() and not self.overwrite:
            raise FileExistsError(
                f"{out} exists; pass overwrite=True (--overwrite) to replace it"
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        shell = self.footer.shell_for(storage_i)
        blob = shell[: self.px_start] + body + shell[self.px_start :]
        out.write_bytes(blob)
        if mtime:
            os.utime(out, (mtime, mtime))
        self.n_written += 1
        self.bytes_written += len(blob)
        return len(blob)


def _write_bin_sidecar(dst: Path, summary: dict, meta: dict, file_log) -> Path:
    """A tiny JSON beside the .bin saying how to read it.

    The whole point of the format is that the file itself carries no header, so the geometry has to
    live somewhere or the bytes are unusable.
    """
    rows, cols = summary["shape"]
    info = {
        "file": dst.name,
        "layout": "frames concatenated, each row-major (rows, cols)",
        "n_frames": summary["n_frames"],
        "rows": rows,
        "cols": cols,
        "dtype": summary["dtype"],
        "bytes_per_sample": np.dtype(summary["dtype"]).itemsize,
        "frame_bytes": summary["frame_bytes"],
        "total_bytes": summary["n_frames"] * summary["frame_bytes"],
        "frame_order": summary["order"],
        "temporal_order_known": summary["temporal_order_known"],
        "source_dtype": summary["source_dtype"],
        "byteswapped_from_source": summary["byteswapped"],
        "source_wfz": summary["src"],
        "source_tar": meta.get("source_name"),
        "numpy": (
            f"np.memmap('{dst.name}', dtype='{summary['dtype']}', mode='r')"
            f".reshape(-1, {rows}, {cols})"
        ),
        "matlab": (
            f"f=fopen('{dst.name}','r','{'b' if summary['dtype'][0] == '>' else 'l'}'); "
            f"d=fread(f,Inf,'*uint16'); fclose(f); "
            f"d=reshape(d,[{cols} {rows} {summary['n_frames']}]);  % note: MATLAB is column-major, "
            f"so this comes out transposed per frame; permute(d,[2 1 3]) to match numpy"
        ),
    }
    out = dst.with_name(dst.name + ".json")
    existed = out.exists()
    out.write_text(json.dumps(info, indent=2), encoding="utf-8")
    filelog.record_write(file_log, out, existed)
    return out
