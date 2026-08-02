"""Compress and decompress a tar of camera frames, losslessly.

The scheme, per archive:

1. Each member is split into pixels and a shell (:mod:`wfcompress.frames`).
2. Always-zero low bits are stripped. Scientific cameras routinely write 9-12 bit samples
   left-shifted into a 16-bit word, and leaving those hard-zero LSBs in place costs JPEG-LS a
   great deal - on real widefield data the difference is x1.63 vs x2.76. The shift is detected,
   recorded, and undone on read.
3. Each frame is encoded with JPEG-LS in lossless mode (``near=0``).
4. Each frame is decoded again immediately and compared with the source. Any difference aborts
   the run before anything is committed.

Nothing here knows about any particular server or directory layout; see :mod:`wfcompress.lab`.
"""

from __future__ import annotations

import hashlib
import time
import zlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import imagecodecs
import numpy as np

from . import container, frames, tarwalk
from .provenance import provenance

DEFAULT_BATCH = 64
DEFAULT_THREADS = 8


class LosslessCheckFailed(RuntimeError):
    """A frame did not survive the encode/decode round trip. Nothing has been committed."""


def _detect_shift(path: Path, entries, layout, probe: int = 400) -> tuple[int, int]:
    """Trailing-zero bit count common to a spread of frames across the archive.

    Sampled, so it is a hypothesis rather than a guarantee - every frame is checked against it
    during encoding, and a violation aborts the run.
    """
    picks = np.unique(np.linspace(0, len(entries) - 1, min(probe, len(entries))).astype(int))
    or_mask = 0
    with open(path, "rb") as fh:
        for i in picks:
            e = entries[i]
            fh.seek(e.data_offset)
            pixels, _ = frames.split(fh.read(e.size), layout)
            or_mask |= int(np.bitwise_or.reduce(pixels.ravel()))
    if or_mask == 0:
        return 0, 0
    return ((or_mask & -or_mask).bit_length() - 1), or_mask.bit_length()


def compress(
    src: str | Path,
    dst: str | Path,
    shape: tuple[int, int] | None = None,
    threads: int = DEFAULT_THREADS,
    batch: int = DEFAULT_BATCH,
    progress: Callable[[int, int, float], None] | None = None,
) -> dict:
    """Compress ``src`` (a tar of frames) to ``dst`` (a .wfz). Returns the metadata dict.

    ``shape`` is required only for archives of headerless raw frames.
    """
    src, dst = Path(src), Path(dst)
    t0 = time.perf_counter()
    source_bytes = src.stat().st_size

    entries = tarwalk.read_entries(src)
    members = [e for e in entries if e.size > 0]
    if not members:
        raise ValueError(f"{src} contains no data members")

    with open(src, "rb") as fh:
        fh.seek(members[0].data_offset)
        layout = frames.detect_layout(fh.read(members[0].size), shape)

    shift, payload_bits = _detect_shift(src, members, layout)
    low_mask = (1 << shift) - 1

    index = np.zeros((len(members), 3), dtype=np.int64)
    # Shells are identical across frames in every session seen so far, so intern them rather than
    # keeping one per frame: at 4.6 KB each, a 680k-frame archive would otherwise hold ~3 GB of
    # duplicate bytes on the heap purely to write one copy out at the end.
    shell_pool: dict[bytes, int] = {}
    shell_ids: list[int] = []
    pixel_hash = hashlib.sha256()
    pos = container.PAYLOAD_START

    def encode(job):
        i, raw = job
        pixels, shell = frames.split(raw, layout)
        if shift and int(np.bitwise_or.reduce(pixels.ravel())) & low_mask:
            raise LosslessCheckFailed(f"frame {i}: low bits set, detected shift {shift} is wrong")
        shifted = (pixels >> shift).astype(np.uint16)
        code = imagecodecs.jpegls_encode(shifted)
        back = imagecodecs.jpegls_decode(code)
        if not np.array_equal(back, shifted):
            raise LosslessCheckFailed(f"frame {i}: JPEG-LS round trip differs")
        restored = (back.astype(np.uint32) << shift).astype(layout.dtype)
        if not np.array_equal(restored, pixels):
            raise LosslessCheckFailed(f"frame {i}: bit-shift round trip differs")
        # The layout was read from the first member and assumed for the rest. Prove per frame that
        # splitting and rejoining reproduces this member exactly, so byte-identical restore is
        # demonstrated rather than inferred.
        if frames.join(restored, shell, layout) != raw:
            raise LosslessCheckFailed(f"frame {i}: member does not reassemble to its original bytes")
        return i, code, shell, pixels.tobytes()

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout, ThreadPoolExecutor(threads) as pool:
        container.write_header(fout)
        for start in range(0, len(members), batch):
            chunk = members[start : start + batch]
            jobs = []
            for i, e in enumerate(chunk, start=start):
                fin.seek(e.data_offset)
                jobs.append((i, fin.read(e.size)))
            for i, code, shell, body in sorted(pool.map(encode, jobs), key=lambda r: r[0]):
                pixel_hash.update(body)
                shell_ids.append(shell_pool.setdefault(shell, len(shell_pool)))
                index[i] = (pos, len(code), zlib.crc32(code))
                fout.write(code)
                pos += len(code)
            if progress:
                done = min(start + batch, len(members))
                progress(done, len(members), time.perf_counter() - t0)

        unique_shells = sorted(shell_pool, key=shell_pool.get)  # type: ignore[arg-type]
        uniform = len(unique_shells) == 1
        # The footer stores varying shells as a flat blob and the reader slices it at a fixed
        # stride, so unequal lengths would silently reassemble into the wrong bytes. Refuse.
        if not uniform and len({len(s) for s in unique_shells}) != 1:
            raise NotImplementedError(
                f"{src.name}: members have {len({len(s) for s in unique_shells})} different "
                "header sizes; the container assumes a fixed shell length"
            )
        meta = {
            "format": "wfz",
            "format_version": 1,
            "codec": "jpegls",
            "lossless": True,
            "near": 0,
            "source_name": src.name,
            "source_bytes": source_bytes,
            "n_frames": len(members),
            "n_tar_entries": len(entries),
            "shape": list(layout.shape),
            "dtype": layout.dtype.str,
            "is_tiff": layout.is_tiff,
            "px_start": layout.px_start,
            "px_len": layout.px_len,
            "shift": shift,
            "payload_bits": max(payload_bits - shift, 0),
            "shells_uniform": uniform,
            "n_distinct_shells": len(unique_shells),
            "shell_len": len(unique_shells[0]),
            "pixels_sha256": pixel_hash.hexdigest(),
            "byte_identical_restore": True,
            "provenance": provenance(),
            "how_to_decompress": (
                "pip install git+https://github.com/SteinmetzLab/widefieldCompress"
                "  then:  wfcompress decompress FILE.wfz OUT.tar"
            ),
        }
        trailer = tarwalk.trailing_bytes(src, entries)
        footer = container.Footer(
            meta=meta,
            index=index,
            tar_headers=b"".join(e.header for e in entries),
            shells=(
                unique_shells[0]
                if uniform
                else b"".join(unique_shells[sid] for sid in shell_ids)
            ),
            trailer=trailer,
        )
        footer_bytes = container.finalise(fout, pos, footer)

    out_bytes = dst.stat().st_size
    meta["output_bytes"] = out_bytes
    meta["footer_bytes"] = footer_bytes
    meta["ratio"] = source_bytes / out_bytes
    meta["elapsed_s"] = time.perf_counter() - t0
    return meta


def decompress(
    src: str | Path,
    dst: str | Path,
    threads: int = DEFAULT_THREADS,
    batch: int = DEFAULT_BATCH,
    progress: Callable[[int, int, float], None] | None = None,
) -> dict:
    """Rebuild the original tar from a .wfz. Verifies the pixel hash before returning."""
    src, dst = Path(src), Path(dst)
    t0 = time.perf_counter()
    footer = container.read_footer(src)
    meta, index = footer.meta, footer.index

    n = meta["n_frames"]
    dtype = np.dtype(meta["dtype"])
    shape = tuple(meta["shape"])
    shift = meta["shift"]
    px_start, px_len = meta["px_start"], meta["px_len"]
    pixel_hash = hashlib.sha256()

    def decode(job):
        i, code = job
        if zlib.crc32(code) != np.uint32(index[i, 2]):
            raise LosslessCheckFailed(f"frame {i}: stored codestream fails its CRC")
        g = imagecodecs.jpegls_decode(code)
        pixels = (g.astype(np.uint32) << shift).astype(dtype).reshape(shape)
        body = pixels.tobytes()
        if len(body) != px_len:
            raise LosslessCheckFailed(f"frame {i}: {len(body)} pixel bytes, expected {px_len}")
        shell = footer.shell_for(i)
        return i, shell[:px_start] + body + shell[px_start:], body

    header_i = 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout, ThreadPoolExecutor(threads) as pool:
        for start in range(0, n, batch):
            jobs = []
            for i in range(start, min(start + batch, n)):
                fin.seek(index[i, 0])
                jobs.append((i, fin.read(index[i, 1])))
            for i, member, body in sorted(pool.map(decode, jobs), key=lambda r: r[0]):
                # re-emit any zero-size entries (directories) that preceded this member
                while True:
                    h = footer.tar_headers[header_i * tarwalk.BLOCK : (header_i + 1) * tarwalk.BLOCK]
                    header_i += 1
                    fout.write(h)
                    if int(h[124:136].rstrip(b"\0 ").decode("ascii", "replace") or "0", 8):
                        break
                fout.write(member)
                pad = (-len(member)) % tarwalk.BLOCK
                if pad:
                    fout.write(b"\0" * pad)
                pixel_hash.update(body)
            if progress:
                progress(min(start + batch, n), n, time.perf_counter() - t0)
        fout.write(footer.trailer)

    if pixel_hash.hexdigest() != meta["pixels_sha256"]:
        raise LosslessCheckFailed(
            f"{src}: pixel SHA-256 does not match the value recorded at compression time"
        )
    return {
        "output_bytes": dst.stat().st_size,
        "source_bytes": meta["source_bytes"],
        "size_matches": dst.stat().st_size == meta["source_bytes"],
        "elapsed_s": time.perf_counter() - t0,
        "n_frames": n,
    }


def sha256_file(path: str | Path, block: int = 1 << 24) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(block):
            h.update(chunk)
    return h.hexdigest()
