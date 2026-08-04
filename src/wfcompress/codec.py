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
import os
import re
import time
import zlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import imagecodecs
import numpy as np

from . import container, frames, tarwalk
from .provenance import provenance

DEFAULT_BATCH = 64
DEFAULT_THREADS = 8

#: Minimum age of a source archive before it is considered finished being written. Data arrives
#: here straight off an acquisition machine, so an archive can genuinely be mid-transfer.
DEFAULT_MIN_AGE_S = 3600


class LosslessCheckFailed(RuntimeError):
    """A frame did not survive the encode/decode round trip. Nothing has been committed."""


class UnsupportedArchive(ValueError):
    """The archive uses a tar layout this container cannot reproduce byte-for-byte.

    Raised during preflight, before anything is written. The alternative would be to emit an
    archive that silently differs from the original, which for a tool whose purpose is provable
    losslessness is the worse outcome.
    """


class SourceChanged(RuntimeError):
    """The source archive was modified while it was being read."""


def _assert_distinct(src: Path, dst: Path) -> None:
    """Refuse to write over our own input.

    ``open(dst, "wb")`` truncates before the first read, so aliasing paths destroy the source
    outright - measured at 10,240 bytes down to 16.
    """
    if dst.exists() and src.exists() and os.path.samefile(src, dst):
        raise ValueError(f"source and destination are the same file: {src}")
    if src.resolve() == dst.resolve():
        raise ValueError(f"source and destination resolve to the same path: {src}")


@contextmanager
def _atomic_output(dst: Path):
    """Write to a temporary file beside the destination and rename only on success.

    A crash, a full disk or a dropped SMB connection must not leave a partial file sitting under
    the final name, where a stale sidecar could vouch for it.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.partial-{os.getpid()}")
    try:
        with open(tmp, "wb") as fh:
            yield fh
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dst)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def _source_fingerprint(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def _temporal_order(names: list[str]) -> np.ndarray | None:
    """Map temporal index -> storage index, from the frame number in each member name.

    These archives are written in lexicographic name order (``frame-0, frame-1, frame-10,
    frame-100``), so storage position is *not* acquisition order. Returns None when the names do
    not carry a usable distinct frame number, in which case storage order is all we know.
    """
    numbers = []
    for name in names:
        m = re.search(r"(\d+)(?:\.[A-Za-z0-9]+)?$", name)
        if not m:
            return None
        numbers.append(int(m.group(1)))
    if len(set(numbers)) != len(numbers):
        return None
    return np.argsort(np.asarray(numbers, dtype=np.int64), kind="stable").astype(np.int64)


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


def _entry_spans(entries, batch: int) -> list[tuple[int, int, int]]:
    """Group entries into contiguous spans of at most ``batch`` data members each.

    Returns (first_entry, last_entry_exclusive, index_of_first_data_member). Spans tile the archive
    with no gaps, so reading them in order is a single sequential pass over the file.
    """
    spans, span_start, in_span, member_i, first_member = [], 0, 0, 0, 0
    for k, e in enumerate(entries):
        if e.size:
            if in_span == 0:
                first_member = member_i
            in_span += 1
            member_i += 1
        if in_span == batch:
            spans.append((span_start, k + 1, first_member))
            span_start, in_span = k + 1, 0
    if span_start < len(entries):
        spans.append((span_start, len(entries), first_member))
    return spans


def _preflight(src: Path, entries, members) -> None:
    """Reject tar layouts this container cannot reproduce exactly, before writing anything.

    Two cases are known to break byte-identical restore, both verified against real tars:

    * a zero-size entry (a directory, say) *after* the last data member - reconstruction emits
      pending headers only while looking for the next member, so a trailing one is dropped;
    * nonzero bytes in a member's alignment padding - reconstruction synthesises zero padding.

    Neither occurs in the widefield corpus, and both are cheap to detect here.
    """
    last_member = max(i for i, e in enumerate(entries) if e.size > 0)
    trailing = [e for e in entries[last_member + 1 :]]
    if trailing:
        raise UnsupportedArchive(
            f"{src.name}: {len(trailing)} zero-size tar entr"
            f"{'y' if len(trailing) == 1 else 'ies'} after the last data member "
            f"(first: {trailing[0].name!r}); reconstruction cannot place them"
        )

    with open(src, "rb") as fh:
        for e in members:
            pad = e.padded_size - e.size
            if not pad:
                continue
            fh.seek(e.data_offset + e.size)
            if fh.read(pad).strip(b"\0"):
                raise UnsupportedArchive(
                    f"{src.name}: member {e.name!r} has nonzero alignment padding, which "
                    f"reconstruction would replace with zeros"
                )

    sizes = {e.size for e in members}
    if len(sizes) != 1:
        raise UnsupportedArchive(
            f"{src.name}: members have {len(sizes)} different sizes ({sorted(sizes)[:4]}...); "
            f"the frame layout is taken from the first member and applied to all"
        )


def compress(
    src: str | Path,
    dst: str | Path,
    shape: tuple[int, int] | None = None,
    threads: int = DEFAULT_THREADS,
    batch: int = DEFAULT_BATCH,
    progress: Callable[[int, int, float], None] | None = None,
    min_age_s: float = 0.0,
) -> dict:
    """Compress ``src`` (a tar of frames) to ``dst`` (a .wfz). Returns the metadata dict.

    ``shape`` is required only for archives of headerless raw frames.

    ``min_age_s`` refuses archives modified more recently than that, for use where data is still
    arriving from an acquisition machine. The source's size and mtime are recorded before the read
    and rechecked after it either way, so a concurrent write is detected rather than baked into a
    self-consistent snapshot of a state the file never had.
    """
    src, dst = Path(src), Path(dst)
    t0 = time.perf_counter()
    _assert_distinct(src, dst)

    fingerprint = _source_fingerprint(src)
    source_bytes = fingerprint[0]
    if min_age_s:
        age = time.time() - src.stat().st_mtime
        if age < min_age_s:
            raise SourceChanged(
                f"{src.name} was modified {age/60:.1f} min ago, less than the {min_age_s/60:.0f} "
                f"min required; it may still be being written"
            )

    entries = tarwalk.read_entries(src)
    members = [e for e in entries if e.size > 0]
    if not members:
        raise ValueError(f"{src} contains no data members")
    _preflight(src, entries, members)

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
        frame_or = int(np.bitwise_or.reduce(pixels.ravel()))
        if shift and frame_or & low_mask:
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
            raise LosslessCheckFailed(
                f"frame {i}: member does not reassemble to its original bytes"
            )
        return i, code, shell, pixels.tobytes(), frame_or

    # Read the source strictly sequentially, in spans covering whole entries, and hash every byte
    # as it goes. That yields the true SHA-256 of the archive for free, which is what lets
    # verification stream the reconstruction and compare hashes instead of writing a restored tar
    # back to the server and re-reading both files (4.9x the source bytes in I/O, vs 1.9x).
    tar_hash = hashlib.sha256()
    observed_or = 0        # OR over *every* frame, not the 400-frame sample
    spans = _entry_spans(entries, batch)

    with open(src, "rb") as fin, _atomic_output(dst) as fout, ThreadPoolExecutor(threads) as pool:
        container.write_header(fout)
        for span_start, span_end, first_member in spans:
            byte_start = entries[span_start].data_offset - tarwalk.BLOCK
            byte_end = entries[span_end - 1].end_offset
            fin.seek(byte_start)
            block = fin.read(byte_end - byte_start)
            if len(block) != byte_end - byte_start:
                raise LosslessCheckFailed(f"{src}: truncated read at offset {byte_start}")
            tar_hash.update(block)

            jobs = []
            i = first_member
            for e in entries[span_start:span_end]:
                if e.size:
                    off = e.data_offset - byte_start
                    jobs.append((i, block[off : off + e.size]))
                    i += 1
            for i, code, shell, body, frame_or in sorted(pool.map(encode, jobs),
                                                          key=lambda r: r[0]):
                observed_or |= frame_or
                pixel_hash.update(body)
                shell_ids.append(shell_pool.setdefault(shell, len(shell_pool)))
                index[i] = (pos, len(code), zlib.crc32(code))
                fout.write(code)
                pos += len(code)
            if progress:
                progress(min(i + 1, len(members)), len(members), time.perf_counter() - t0)

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
            "format_version": 2,
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
            # from every frame, not the sampled subset that chose the shift
            "payload_bits": max(observed_or.bit_length() - shift, 0),
            "sampled_payload_bits": max(payload_bits - shift, 0),
            "observed_or_mask": observed_or,
            "shift_could_have_been": (
                ((observed_or & -observed_or).bit_length() - 1) if observed_or else 0
            ),
            "shells_uniform": uniform,
            "n_distinct_shells": len(unique_shells),
            "shell_len": len(unique_shells[0]),
            "pixels_sha256": pixel_hash.hexdigest(),
            "source_tar_sha256": None,  # filled in below, once the trailer has been hashed
            # Not asserted here. Every frame round-tripped and every member reassembled to its
            # original bytes, but only verify() hashes the whole rebuilt archive end to end.
            "byte_identical_verified": False,
            "provenance": provenance(),
            "how_to_decompress": (
                "pip install git+https://github.com/SteinmetzLab/widefieldCompress"
                "  then:  wfcompress decompress FILE.wfz OUT.tar"
            ),
        }
        trailer = tarwalk.trailing_bytes(src, entries)
        tar_hash.update(trailer)
        meta["source_tar_sha256"] = tar_hash.hexdigest()

        # The source was opened several times (headers, shift sample, spans, trailer). If it
        # changed under us, the hash above describes a state the file never actually had.
        if _source_fingerprint(src) != fingerprint:
            raise SourceChanged(
                f"{src.name} changed while it was being compressed; nothing has been committed"
            )

        order = _temporal_order([e.name for e in members])
        meta["temporal_order_known"] = order is not None
        if order is None:
            meta["temporal_order_note"] = (
                "member names carry no distinct trailing frame number, so acquisition order "
                "could not be recovered; frame(i) returns storage order"
            )
        footer = container.Footer(
            meta=meta,
            index=index,
            order=order,
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


def iter_tar_bytes(
    src: str | Path,
    threads: int = DEFAULT_THREADS,
    batch: int = DEFAULT_BATCH,
    progress: Callable[[int, int, float], None] | None = None,
):
    """Yield the reconstructed original archive as a stream of byte chunks.

    Both :func:`decompress` and :func:`verify` go through this, so there is exactly one
    implementation of the reconstruction and they cannot drift apart. The pixel SHA-256 is checked
    against the recorded value when the stream is exhausted; nothing is yielded but bytes.
    """
    src = Path(src)
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
    with open(src, "rb") as fin, ThreadPoolExecutor(threads) as pool:
        for start in range(0, n, batch):
            jobs = []
            for i in range(start, min(start + batch, n)):
                fin.seek(index[i, 0])
                jobs.append((i, fin.read(index[i, 1])))
            for _i, member, body in sorted(pool.map(decode, jobs), key=lambda r: r[0]):
                # re-emit any zero-size entries (directories) that preceded this member
                while True:
                    h = footer.tar_headers[
                        header_i * tarwalk.BLOCK : (header_i + 1) * tarwalk.BLOCK
                    ]
                    header_i += 1
                    yield h
                    if int(h[124:136].rstrip(b"\0 ").decode("ascii", "replace") or "0", 8):
                        break
                yield member
                pad = (-len(member)) % tarwalk.BLOCK
                if pad:
                    yield b"\0" * pad
                pixel_hash.update(body)
            if progress:
                progress(min(start + batch, n), n, time.perf_counter() - t0)
        yield footer.trailer

    if pixel_hash.hexdigest() != meta["pixels_sha256"]:
        raise LosslessCheckFailed(
            f"{src}: pixel SHA-256 does not match the value recorded at compression time"
        )


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
    _assert_distinct(src, dst)
    meta = container.read_meta(src)

    # Every byte passes through here anyway, so hashing costs almost nothing and turns
    # decompression into its own proof rather than something a separate check has to confirm.
    h = hashlib.sha256()
    with _atomic_output(dst) as fout:
        for chunk in iter_tar_bytes(src, threads, batch, progress):
            h.update(chunk)
            fout.write(chunk)

    digest = h.hexdigest()
    expected = meta.get("source_tar_sha256")
    if expected is not None and digest != expected:
        dst.unlink(missing_ok=True)
        raise LosslessCheckFailed(
            f"{src}: rebuilt archive hashes {digest}, expected {expected}"
        )
    return {
        "output_bytes": dst.stat().st_size,
        "source_bytes": meta["source_bytes"],
        "size_matches": dst.stat().st_size == meta["source_bytes"],
        "tar_sha256": digest,
        "byte_identical": None if expected is None else digest == expected,
        "elapsed_s": time.perf_counter() - t0,
        "n_frames": meta["n_frames"],
    }


def verify(
    src: str | Path,
    threads: int = DEFAULT_THREADS,
    batch: int = DEFAULT_BATCH,
    progress: Callable[[int, int, float], None] | None = None,
) -> dict:
    """Prove a .wfz rebuilds the original archive, **without writing anything**.

    Streams the reconstruction through SHA-256 and compares with the hash taken from the source
    file during compression. Equivalent in strength to decompressing and diffing, at a fraction of
    the I/O: reads only the .wfz, where the decompress-and-compare route reads and writes the full
    uncompressed archive as well.
    """
    src = Path(src)
    t0 = time.perf_counter()
    meta = container.read_meta(src)
    expected = meta.get("source_tar_sha256")

    h = hashlib.sha256()
    total = 0
    for chunk in iter_tar_bytes(src, threads, batch, progress):
        h.update(chunk)
        total += len(chunk)
    digest = h.hexdigest()

    result = {
        "wfz": str(src),
        "rebuilt_bytes": total,
        "source_bytes": meta["source_bytes"],
        "size_matches": total == meta["source_bytes"],
        "tar_sha256": digest,
        "expected_tar_sha256": expected,
        "byte_identical": (expected is not None and digest == expected),
        "elapsed_s": time.perf_counter() - t0,
    }
    if expected is None:
        # written by a build that predates source_tar_sha256; the pixel hash was still checked
        result["byte_identical"] = None
    elif digest != expected:
        raise LosslessCheckFailed(
            f"{src}: rebuilt archive hashes {digest}, expected {expected}"
        )
    if not result["size_matches"]:
        raise LosslessCheckFailed(
            f"{src}: rebuilt {total} bytes, source was {meta['source_bytes']}"
        )
    return result


def sha256_file(path: str | Path, block: int = 1 << 24) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(block):
            h.update(chunk)
    return h.hexdigest()
