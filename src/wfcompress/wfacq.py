"""wfAcq session folders to .wfz and back, losslessly.

A wfAcq session is a folder holding ``wf.bin`` (raw ``uint16`` frames, little-endian, C order, no
header) and a few small files that describe it. The format is defined in wfAcq's ``FORMAT.md``,
which allows direct parsing by code that follows it; this module does, so wfcompress does not
depend on wfacq.

What goes into the .wfz:

- every frame of ``wf.bin``, JPEG-LS lossless, in file order. ``wf.bin`` is the pixel stream and
  nothing else, so the decoded frames concatenated *are* ``wf.bin``, byte for byte;
- the small session files, verbatim (zstd inside the footer), so the frames can never be separated
  from the frames table that gives them counters, timestamps, labels and flags.

Only sessions that closed cleanly are accepted. For those, FORMAT.md guarantees that ``wf.bin``
holds exactly ``n_frames`` whole frames and both frames tables exactly ``n_frames`` records. A
session that did not close cleanly is read by rules that can leave a partial frame or unrecorded
frames at the end of ``wf.bin``, so a .wfz of the readable frames would not rebuild the file. Those
are refused, for a person to decide about.

Geometry comes from ``wf_meta.json``, never from the size of ``wf.bin``: a wrong factorisation of
the file size produces a plausible-looking image, not an error. The bit shift is fixed at 0 from
metadata (``pixel_shift_bits``) rather than detected by sampling, as the tar path does.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import time
import zipfile
import zlib
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import imagecodecs
import numpy as np

from . import container, filelog, sidecar
from .codec import (
    DEFAULT_BATCH,
    DEFAULT_MIN_AGE_S,
    DEFAULT_THREADS,
    WRITE_BUFFER,
    LosslessCheckFailed,
    SourceChanged,
    _assert_distinct,
    _atomic_output,
    _commit_no_clobber,
    _partial_name,
)
from .provenance import provenance

BIN = "wf.bin"
#: Every small file a session can hold, in FORMAT.md's order. All are stored in the .wfz.
SESSION_FILES = ("wf_meta.json", "wf_frames.npy", "wf_frames.raw", "wf_events.jsonl",
                 "wf_camera.pfs")
#: Written for camera sessions only (FORMAT.md, "Session folder").
OPTIONAL_FILES = frozenset({"wf_camera.pfs"})
META_TMP = "wf_meta.json.tmp"

#: FORMAT.md, "Frames table": packed little-endian records of 30 bytes.
RECORD_FIELDS = ("frame_counter", "block_id", "camera_timestamp", "channel", "flags", "roi_mean")
RECORD_BYTES = 30

#: wfAcq format versions this reader follows. wfAcq writes 0 until the camera-dependent parts are
#: confirmed at the rig; FORMAT.md calls those sessions test data whose layout may still change.
WFACQ_FORMAT_VERSIONS = (0, 1)

#: Format version of the .wfz written for a session. Tar sources stay at 2.
FORMAT_VERSION = 3

#: The name DataBrowser looks for in a session folder.
DEFAULT_NAME = "widefield.wfz"


class SessionRefused(ValueError):
    """The folder is not a finished, cleanly closed wfAcq session. Nothing has been written."""


@dataclass
class Session:
    path: Path
    meta: dict
    shape: tuple[int, int]
    n_frames: int
    bin_bytes: int
    files: list[str]  # the session files present, besides wf.bin
    fingerprint: dict = field(repr=False, default_factory=dict)
    data: dict = field(repr=False, default_factory=dict)  # their bytes, read once

    @property
    def frame_bytes(self) -> int:
        return self.shape[0] * self.shape[1] * 2


def _fingerprint(path: Path, names) -> dict:
    """(size, mtime_ns) of each file, plus whether the metadata temporary exists.

    Checked before and after the read: if any of it changed, the hashes describe a state the
    session never had.
    """
    fp = {}
    for name in names:
        st = (path / name).stat()
        fp[name] = (st.st_size, st.st_mtime_ns)
    fp[META_TMP] = (path / META_TMP).exists()
    return fp


def _exact(value, want) -> bool:
    # `type` too, so that False does not pass for 0 or 1.0 for 1
    return type(value) is type(want) and value == want


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


def _read_session_files(path: Path, names) -> dict[str, bytes]:
    """The small session files, read once. What is validated is exactly what gets stored."""
    return {name: (path / name).read_bytes() for name in names}


#: Older than this is not a real recording time. robocopy stamps a file it has not finished copying
#: 1980-01-01 while it already has its full size, which would pass both the size and age checks.
_IMPLAUSIBLE_MTIME_NS = 946684800 * 10**9  # 2000-01-01 UTC


def inspect_session(path: str | Path, min_age_s: float = DEFAULT_MIN_AGE_S) -> Session:
    """Check that ``path`` is a finished, cleanly closed session. Raises :class:`SessionRefused`.

    Reads the metadata and the frames tables, never ``wf.bin``'s contents. The protection that
    matters is ``closed_cleanly`` together with ``wf.bin``'s size matching ``bin_bytes``; the
    minimum age is a second line of defense, and it compares this machine's clock with the file
    server's, so it is only as good as their agreement.
    """
    path = Path(path)

    def refuse(why: str):
        raise SessionRefused(f"{path}: {why}")

    if not path.is_dir():
        refuse("not a directory")
    if (path / META_TMP).exists():
        refuse(f"{META_TMP} exists, so which metadata is final is ambiguous (FORMAT.md, "
               f"'wf_meta.json'); the session did not finish closing. Leave it for manual review.")
    missing = [n for n in (BIN, *SESSION_FILES)
               if n not in OPTIONAL_FILES and not (path / n).is_file()]
    if missing:
        refuse(f"not a finished wfAcq session folder: missing {', '.join(missing)}")

    # Fingerprint before reading anything, so a change during the read is caught at the end.
    files = [name for name in SESSION_FILES if (path / name).is_file()]
    fingerprint = _fingerprint(path, [BIN, *files])
    data = _read_session_files(path, files)

    try:
        meta = json.loads(data["wf_meta.json"].decode("utf-8"))
    except ValueError as e:
        refuse(f"wf_meta.json cannot be parsed: {e}")
    if not isinstance(meta, dict):
        refuse("wf_meta.json is not a JSON object")

    version = meta.get("format_version")
    if type(version) is not int or version not in WFACQ_FORMAT_VERSIONS:
        refuse(f"wfAcq format_version {version!r} is not one this reader follows "
               f"({WFACQ_FORMAT_VERSIONS}); a newer wfcompress may be needed")
    if meta.get("closed_cleanly") is not True:
        refuse(f"did not close cleanly (closed_cleanly {meta.get('closed_cleanly')!r}, "
               f"close_error {meta.get('close_error')!r}). wf.bin may hold a partial frame or "
               f"frames with no record, so a .wfz could not rebuild it. Leave wf.bin in place for "
               f"manual review (FORMAT.md, 'Number of frames').")
    for key, want in (("dtype", "uint16"), ("byte_order", "little"), ("frame_order", "C"),
                      ("pixel_shift_bits", 0)):
        if not _exact(meta.get(key), want):
            refuse(f"{key} is {meta.get(key)!r}, expected {want!r}")

    h, w, n, bin_bytes = (meta.get(k) for k in ("height", "width", "n_frames", "bin_bytes"))
    if not (_positive_int(h) and _positive_int(w)):
        refuse(f"geometry height={h!r} width={w!r} is not two positive integers")
    if n == 0:
        refuse("the session recorded no frames; nothing to compress")
    if not _positive_int(n):
        refuse(f"n_frames is {n!r}; the frame count is unknown")
    if type(bin_bytes) is not int:
        refuse(f"bin_bytes is {bin_bytes!r}; a cleanly closed session records it")
    frame_bytes = h * w * 2
    if bin_bytes != n * frame_bytes:
        refuse(f"bin_bytes {bin_bytes:,} is not n_frames x height x width x 2 = "
               f"{n * frame_bytes:,}")

    size = fingerprint[BIN][0]
    if size != bin_bytes:
        refuse(f"{BIN} is {size:,} bytes but bin_bytes is {bin_bytes:,}: an incomplete copy, or "
               f"changed after the session closed")
    raw = np.frombuffer(data["wf_frames.raw"], dtype=np.uint8)
    if raw.size != n * RECORD_BYTES:
        refuse(f"wf_frames.raw is {raw.size:,} bytes, expected {n} records of {RECORD_BYTES}")
    try:
        table = np.load(io.BytesIO(data["wf_frames.npy"]), allow_pickle=False)
    except Exception as e:  # noqa: BLE001 - any failure to parse is the answer
        refuse(f"wf_frames.npy cannot be read: {type(e).__name__}: {e}")
    if table.dtype.names != RECORD_FIELDS or table.dtype.itemsize != RECORD_BYTES:
        refuse(f"wf_frames.npy has records {table.dtype}, not FORMAT.md's 30-byte layout")
    if table.shape != (n,):
        refuse(f"wf_frames.npy holds {table.shape} records, metadata says {n}")
    if not np.array_equal(np.ascontiguousarray(table).view(np.uint8).ravel(), raw):
        refuse("wf_frames.npy and wf_frames.raw disagree; FORMAT.md says they hold the same "
               "records")

    mtimes = [size_mtime[1] for k, size_mtime in fingerprint.items() if k != META_TMP]
    if min(mtimes) < _IMPLAUSIBLE_MTIME_NS:
        refuse("a session file has a modification time before 2000, which is how robocopy marks a "
               "file it has not finished copying (1980-01-01). Let the copy finish.")
    if min_age_s:
        age = time.time() - max(mtimes) / 1e9
        if age < min_age_s:
            refuse(f"a session file was modified {age:.0f} s ago, less than the {min_age_s:.0f} s "
                   f"required; it may still be being written or copied. Wait, or pass "
                   f"min_age_s=0 (--min-age-s 0) if you know the copy is complete.")

    return Session(path=path, meta=meta, shape=(h, w), n_frames=n, bin_bytes=bin_bytes,
                   files=files, fingerprint=fingerprint, data=data)


def _read_exactly(fh, n: int) -> bytes:
    buf = bytearray(n)
    view, got = memoryview(buf), 0
    while got < n:
        k = fh.readinto(view[got:])
        if not k:
            raise LosslessCheckFailed(f"{fh.name}: truncated read, {got:,} of {n:,} bytes")
        got += k
    return bytes(buf)


def compress_session(
    session: str | Path,
    dst: str | Path,
    threads: int = DEFAULT_THREADS,
    batch: int = DEFAULT_BATCH,
    progress: Callable[[int, int, float], None] | None = None,
    min_age_s: float = DEFAULT_MIN_AGE_S,
    file_log: str | None = None,
) -> dict:
    """Compress a cleanly closed wfAcq session to ``dst``. Returns the metadata dict.

    Never modifies the session, and refuses to replace an existing ``dst``. Every frame is encoded,
    decoded and compared with the source before it is written, and ``wf.bin`` is hashed as it is
    read, so :func:`verify_session` can later prove byte-identity from the .wfz alone.
    """
    t0 = time.perf_counter()
    s = inspect_session(session, min_age_s)
    dst = Path(dst)
    for name in (BIN, *s.files):
        _assert_distinct(s.path / name, dst)
    if dst.exists():
        raise FileExistsError(f"{dst} exists; refusing to replace it. Choose another path, or "
                              f"remove it yourself if it is not needed.")
    receipt = sidecar.receipt_path(dst)
    if receipt.exists():
        # A receipt left from an earlier run would vouch for a new, unverified file.
        raise FileExistsError(f"{receipt} exists without its .wfz; refusing to write a new .wfz "
                              f"beside a receipt that does not describe it. Remove it first.")

    h, w = s.shape
    n, frame_bytes = s.n_frames, s.frame_bytes
    session_files = s.data

    def encode(job):
        i, frame = job
        code = imagecodecs.jpegls_encode(frame)
        back = imagecodecs.jpegls_decode(code)
        if not np.array_equal(back, frame):
            raise LosslessCheckFailed(f"frame {i}: JPEG-LS round trip differs")
        return i, code, int(np.bitwise_or.reduce(frame, axis=None))

    index = np.zeros((n, 3), dtype=np.int64)
    bin_hash = hashlib.sha256()
    observed_or = 0
    pos = container.PAYLOAD_START

    with (
        open(s.path / BIN, "rb", buffering=0) as fin,
        _atomic_output(dst, file_log, no_clobber=True) as fout,
        ThreadPoolExecutor(threads) as pool,
        ThreadPoolExecutor(1) as reader,
    ):
        container.write_header(fout)

        def read(start):
            return _read_exactly(fin, min(batch, n - start) * frame_bytes)

        # Read the next block while this one encodes: over the network the read is not free.
        pending = reader.submit(read, 0)
        for start in range(0, n, batch):
            block = pending.result()
            if start + batch < n:
                pending = reader.submit(read, start + batch)
            bin_hash.update(block)
            stack = np.frombuffer(block, dtype="<u2").reshape(-1, h, w)
            for i, code, frame_or in pool.map(encode, ((start + k, f) for k, f in
                                                       enumerate(stack))):
                observed_or |= frame_or
                index[i] = (pos, len(code), zlib.crc32(code))
                fout.write(code)
                pos += len(code)
            if progress:
                progress(min(start + batch, n), n, time.perf_counter() - t0)
        if fin.read(1):
            raise SourceChanged(f"{s.path / BIN} grew while it was being compressed")

        if _fingerprint(s.path, [BIN, *s.files]) != s.fingerprint:
            raise SourceChanged(
                f"{s.path} changed while it was being compressed; nothing has been committed"
            )
        bin_sha256 = bin_hash.hexdigest()
        m = s.meta
        meta = {
            "format": "wfz",
            "format_version": FORMAT_VERSION,
            "source_kind": container.WFACQ_BIN,
            "codec": "jpegls",
            "lossless": True,
            "near": 0,
            "source_name": BIN,
            "source_bytes": s.bin_bytes,
            "source_bin_bytes": s.bin_bytes,
            "source_bin_sha256": bin_sha256,
            # wf.bin is nothing but the pixels, in order, so the two hashes are the same digest.
            # Both are recorded so readers that know only the pixel hash keep working.
            "pixels_sha256": bin_sha256,
            # None so that tar deletion checks, which require this hash, always refuse this file.
            "source_tar_sha256": None,
            "partial": False,
            "n_frames": n,
            "shape": [h, w],
            "dtype": "<u2",
            "is_tiff": False,
            "px_start": 0,
            "px_len": frame_bytes,
            "shift": 0,
            "shift_source": "wf_meta.json pixel_shift_bits",
            "payload_bits": observed_or.bit_length(),
            "observed_or_mask": observed_or,
            "shift_could_have_been": (
                ((observed_or & -observed_or).bit_length() - 1) if observed_or else 0
            ),
            "temporal_order_known": True,
            "value_units": "camera native, as delivered (no shift or scaling)",
            "wfacq": {
                k: m.get(k)
                for k in ("session_id", "format_version", "wfacq_version", "closed_cleanly",
                          "subject", "session_date", "session_number", "pixel_format",
                          "effective_bit_depth", "frame_rate", "start_time_utc")
            },
            "session_files": {
                name: {
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "mtime_ns": s.fingerprint[name][1],
                }
                for name, data in session_files.items()
            },
            "source_bin_mtime_ns": s.fingerprint[BIN][1],
            # Not asserted here; verify_session() streams the rebuild through the hash.
            "byte_identical_verified": False,
            "provenance": provenance(),
            "how_to_decompress": (
                "pip install git+https://github.com/SteinmetzLab/widefieldCompress"
                "  then:  wfcompress restore FILE.wfz OUTDIR/   (writes wf.bin and the session "
                "files, byte-identical)"
            ),
        }
        footer = container.Footer(
            meta=meta,
            index=index,
            order=np.arange(n, dtype=np.int64),
            tar_headers=b"",
            shells=b"",
            trailer=b"",
            session_files=session_files,
        )
        footer_bytes = container.finalise(fout, pos, footer)

    out_bytes = dst.stat().st_size
    meta["output_bytes"] = out_bytes
    meta["footer_bytes"] = footer_bytes
    meta["ratio"] = s.bin_bytes / out_bytes
    meta["elapsed_s"] = time.perf_counter() - t0
    return meta


def _require_session_wfz(meta: dict, path) -> None:
    if not container.is_wfacq(meta):
        raise ValueError(f"{path} was made from a tar, not a wfAcq session; use decompress, "
                         f"extract or check")


def _iter_frames(
    src: Path,
    footer: container.Footer,
    threads: int,
    batch: int,
    progress: Callable[[int, int, float], None] | None,
    payload_hash=None,
) -> Iterator[bytes]:
    """Yield each frame's ``wf.bin`` bytes, in order, checking every codestream's CRC.

    The codestreams are checked to lie back to back from the start of the payload, so feeding each
    block read to ``payload_hash`` hashes the payload exactly.
    """
    t0 = time.perf_counter()
    meta, index = footer.meta, footer.index
    n, shape, px_len = int(meta["n_frames"]), tuple(meta["shape"]), int(meta["px_len"])
    if index.shape != (n, 3):
        raise LosslessCheckFailed(f"{src}: index is {index.shape}, expected ({n}, 3)")
    ends = index[:, 0] + index[:, 1]
    if n and (np.any(index[1:, 0] != ends[:-1]) or index[0, 0] != container.PAYLOAD_START):
        raise LosslessCheckFailed(f"{src}: codestreams are not stored back to back in order")

    def decode(job):
        i, code = job
        if zlib.crc32(code) != np.uint32(index[i, 2]):
            raise LosslessCheckFailed(f"frame {i}: stored codestream fails its CRC")
        g = imagecodecs.jpegls_decode(code)
        if g.shape != shape:
            raise LosslessCheckFailed(f"frame {i}: decoded {g.shape}, expected {shape}")
        body = g.astype("<u2", copy=False).tobytes()
        if len(body) != px_len:
            raise LosslessCheckFailed(f"frame {i}: {len(body)} bytes, expected {px_len}")
        return body

    with open(src, "rb") as fin, ThreadPoolExecutor(threads) as pool:
        for start in range(0, n, batch):
            stop = min(start + batch, n)
            lo = int(index[start, 0])
            fin.seek(lo)
            blob = _read_exactly(fin, int(ends[stop - 1]) - lo)
            if payload_hash is not None:
                payload_hash.update(blob)
            jobs = [(i, blob[int(index[i, 0]) - lo : int(ends[i]) - lo])
                    for i in range(start, stop)]
            yield from pool.map(decode, jobs)
            if progress:
                progress(stop, n, time.perf_counter() - t0)


def _check_session_file(src, meta: dict, name: str) -> bytes:
    data = container.read_session_file(src, name)
    rec = meta["session_files"][name]
    if len(data) != rec["bytes"] or hashlib.sha256(data).hexdigest() != rec["sha256"]:
        raise LosslessCheckFailed(f"{src}: stored {name} does not match its recorded SHA-256")
    return data


def _check_stored_files(src, meta: dict) -> dict[str, bytes]:
    """Every stored session file, hash-checked, and consistent with the footer's own metadata.

    The per-file hashes live in the same metadata they would vouch for, so on their own they cannot
    show that a file is missing from the list. The stored wf_meta.json is an independent record of
    the session, so it is cross-checked against what the footer claims.
    """
    stored = meta.get("session_files", {})
    unknown = sorted(set(stored) - set(SESSION_FILES))
    if unknown:
        raise LosslessCheckFailed(f"{src}: unexpected stored file name(s) {unknown}")
    missing = [n for n in SESSION_FILES if n not in OPTIONAL_FILES and n not in stored]
    if missing:
        raise LosslessCheckFailed(f"{src}: session files missing from the footer: {missing}")
    files = {name: _check_session_file(src, meta, name) for name in stored}

    m = json.loads(files["wf_meta.json"].decode("utf-8"))
    expect = {
        "n_frames": meta["n_frames"],
        "bin_bytes": meta["source_bin_bytes"],
        "height": meta["shape"][0],
        "width": meta["shape"][1],
        "session_id": meta.get("wfacq", {}).get("session_id"),
        "closed_cleanly": True,
    }
    wrong = {k: (m.get(k), v) for k, v in expect.items() if not _exact(m.get(k), v)}
    if wrong:
        raise LosslessCheckFailed(
            f"{src}: the stored wf_meta.json disagrees with the footer: "
            + ", ".join(f"{k} {a!r} vs {b!r}" for k, (a, b) in wrong.items())
        )
    if len(files["wf_frames.raw"]) != meta["n_frames"] * RECORD_BYTES:
        raise LosslessCheckFailed(f"{src}: stored wf_frames.raw does not hold n_frames records")
    return files


def verify_session(
    src: str | Path,
    threads: int = DEFAULT_THREADS,
    batch: int = DEFAULT_BATCH,
    progress: Callable[[int, int, float], None] | None = None,
) -> dict:
    """Prove a session .wfz rebuilds ``wf.bin`` and every session file, **writing nothing**.

    Streams the decoded frames through SHA-256 and compares with the hash of ``wf.bin`` taken while
    it was compressed. Raises :class:`LosslessCheckFailed` on any difference.

    This proves the .wfz holds exactly the bytes that were read from ``wf.bin``. It does not prove
    that ``wf.bin`` on disk still holds them, or that it was complete when it was read; anything
    that decides to delete ``wf.bin`` must also re-hash it and compare with ``source_bin_sha256``.
    """
    src = Path(src)
    t0 = time.perf_counter()
    footer = container.read_footer(src)
    meta = footer.meta
    _require_session_wfz(meta, src)
    files = _check_stored_files(src, meta)

    # The whole .wfz is hashed on the way, almost for free, so a receipt can name the exact file it
    # describes: header, then the payload as the frames are read, then the footer.
    with open(src, "rb") as fh:
        header = fh.read(container.HEADER_LEN)
        footer_offset = struct.unpack("<Q", header[len(container.MAGIC):])[0]
        payload_end = int(footer.index[-1, 0] + footer.index[-1, 1])
        if payload_end != footer_offset:
            raise LosslessCheckFailed(f"{src}: payload ends at {payload_end:,}, footer starts at "
                                      f"{footer_offset:,}")
        fh.seek(footer_offset)
        footer_bytes = fh.read()
    whole = hashlib.sha256(header)

    h, total = hashlib.sha256(), 0
    for body in _iter_frames(src, footer, threads, batch, progress, payload_hash=whole):
        h.update(body)
        total += len(body)
    whole.update(footer_bytes)
    digest = h.hexdigest()
    expected, expect_bytes = meta["source_bin_sha256"], meta["source_bin_bytes"]
    if total != expect_bytes:
        raise LosslessCheckFailed(f"{src}: rebuilt {total:,} bytes, expected {expect_bytes:,}")
    if digest != expected:
        raise LosslessCheckFailed(f"{src}: rebuilt wf.bin hashes {digest}, expected {expected}")

    return {
        "wfz": str(src),
        "rebuilt_bytes": total,
        "source_bytes": meta["source_bytes"],
        "partial": False,
        "size_matches": True,
        "bin_sha256": digest,
        "expected_bin_sha256": expected,
        "byte_identical": True,
        "session_files_ok": True,
        "n_session_files": len(files),
        "wfz_sha256": whole.hexdigest(),
        "wfz_bytes": container.HEADER_LEN + (payload_end - container.PAYLOAD_START)
        + len(footer_bytes),
        "footer_bytes": len(footer_bytes),
        "elapsed_s": time.perf_counter() - t0,
    }


def restore_session(
    src: str | Path,
    out_dir: str | Path,
    threads: int = DEFAULT_THREADS,
    batch: int = DEFAULT_BATCH,
    progress: Callable[[int, int, float], None] | None = None,
    file_log: str | None = None,
    only_bin: bool = False,
) -> dict:
    """Write ``wf.bin`` and the session files from a session .wfz into ``out_dir``.

    Refuses if any of those files already exists there, including one that appears while the
    rebuild runs: this must never write over a recording. Everything is written to temporary names
    and hash-checked first, then renamed into place together; if anything fails, nothing this call
    wrote is left behind. Files keep their original modification times.

    ``only_bin`` writes ``wf.bin`` alone, for putting it back into a session folder whose small
    files are still there.
    """
    src, out_dir = Path(src), Path(out_dir)
    t0 = time.perf_counter()
    footer = container.read_footer(src)
    meta = footer.meta
    _require_session_wfz(meta, src)
    files = _check_stored_files(src, meta)
    if only_bin:
        # wf.bin alone goes back only beside the metadata of the recording it came from; anywhere
        # else it would make a folder whose wf.bin contradicts its own wf_meta.json.
        local = out_dir / "wf_meta.json"
        if not local.is_file() or local.read_bytes() != files["wf_meta.json"]:
            raise LosslessCheckFailed(
                f"{out_dir}: wf_meta.json is missing or is not the one stored in {src.name}, so "
                f"this is not the folder of that recording; refusing to restore wf.bin into it"
            )
        files = {}
    names = [BIN, *files]

    clash = [name for name in names if (out_dir / name).exists()]
    if clash:
        hint = "" if only_bin else ", or restore wf.bin alone with only_bin (--bin-only)"
        raise FileExistsError(f"{out_dir} already holds {', '.join(clash)}; refusing to replace "
                              f"anything. Restore into a new, empty folder{hint}.")
    out_dir.mkdir(parents=True, exist_ok=True)

    staged: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    h = hashlib.sha256()
    try:
        tmp = _partial_name(out_dir / BIN, no_clobber=True)
        staged.append((tmp, out_dir / BIN))
        filelog.record(file_log, "create", tmp, size_bytes=0, note="temporary output")
        with open(tmp, "wb", buffering=WRITE_BUFFER) as fout:
            for body in _iter_frames(src, footer, threads, batch, progress):
                h.update(body)
                fout.write(body)
            fout.flush()
            os.fsync(fout.fileno())
        if h.hexdigest() != meta["source_bin_sha256"]:
            raise LosslessCheckFailed(f"{src}: rebuilt wf.bin does not match its recorded hash")
        for name, data in files.items():
            tmp = _partial_name(out_dir / name, no_clobber=True)
            staged.append((tmp, out_dir / name))
            filelog.record(file_log, "create", tmp, size_bytes=0, note="temporary output")
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        for tmp, dst in staged:
            _commit_no_clobber(tmp, dst)
            committed.append(dst)
            filelog.record(file_log, "delete", tmp, note="renamed into place")
            filelog.record_write(file_log, dst, False)
    except BaseException:
        # Only files this call created: its own temporaries, and targets it renamed into place.
        for tmp, _dst in staged:
            if tmp.exists():
                filelog.record(file_log, "delete", tmp, note="discarded, restore failed")
                tmp.unlink()
        for dst in committed:
            filelog.record(file_log, "delete", dst, note="rolled back, restore failed")
            dst.unlink()
        raise

    _set_mtime(out_dir / BIN, meta.get("source_bin_mtime_ns"))
    for name in files:
        _set_mtime(out_dir / name, meta["session_files"][name].get("mtime_ns"))
    return {
        "wfz": str(src),
        "out_dir": str(out_dir),
        "files": names,
        "bin_sha256": h.hexdigest(),
        "byte_identical": True,
        "elapsed_s": time.perf_counter() - t0,
    }


def _set_mtime(path: Path, mtime_ns) -> None:
    if mtime_ns:
        os.utime(path, ns=(int(mtime_ns), int(mtime_ns)))


def convert_session(
    session: str | Path,
    dst: str | Path | None = None,
    threads: int = DEFAULT_THREADS,
    batch: int = DEFAULT_BATCH,
    progress: Callable[[int, int, float], None] | None = None,
    verify_progress: Callable[[int, int, float], None] | None = None,
    min_age_s: float = DEFAULT_MIN_AGE_S,
    file_log: str | None = None,
) -> dict:
    """Compress, verify by streaming the rebuild, then write the README, preview and receipt.

    ``dst`` defaults to ``widefield.wfz`` inside the session folder, where DataBrowser looks. The
    receipt claims byte-identity only once :func:`verify_session` has proved it. Nothing is ever
    deleted here.
    """
    t0 = time.perf_counter()
    session = Path(session)
    dst = Path(dst) if dst is not None else session / DEFAULT_NAME
    meta = compress_session(session, dst, threads=threads, batch=batch, progress=progress,
                            min_age_s=min_age_s, file_log=file_log)
    v = verify_session(dst, threads=threads, batch=batch, progress=verify_progress)
    _write_sidecars(dst, meta, v, file_log)
    return _result(session, dst, meta, v, meta["elapsed_s"], t0)


def finish_session(
    session: str | Path,
    dst: str | Path | None = None,
    threads: int = DEFAULT_THREADS,
    batch: int = DEFAULT_BATCH,
    verify_progress: Callable[[int, int, float], None] | None = None,
    min_age_s: float = DEFAULT_MIN_AGE_S,
    file_log: str | None = None,
) -> dict:
    """Finish a :func:`convert_session` that stopped after its .wfz was in place but before the
    receipt: verify the .wfz, then write the README, preview and receipt as ``convert_session``
    does. Nothing is compressed again, and nothing is deleted.

    The .wfz is renamed into place only once compression has finished, so what is left is the
    verification. Refuses (:class:`SessionRefused`) unless the .wfz is of this session as it is now:
    the same ``session_id``, ``wf.bin`` the same size and modification time as when it was read,
    and the same small session files, byte for byte. Refuses (``FileExistsError``) a .wfz that
    already has its receipt. A .wfz that fails verification raises
    :class:`LosslessCheckFailed`, and no receipt is written; so does a file that is not a complete
    session .wfz. The receipt is ``convert_session``'s without ``elapsed_s`` (nothing was
    compressed), with ``finished_after_stop: true``.
    """
    t0 = time.perf_counter()
    session = Path(session)
    dst = Path(dst) if dst is not None else session / DEFAULT_NAME
    if not dst.is_file():
        raise FileNotFoundError(f"{dst} not found: nothing to finish")
    if sidecar.receipt_path(dst).exists():
        raise FileExistsError(f"{dst} already has its receipt: nothing to finish")
    s = inspect_session(session, min_age_s)
    try:
        meta = container.read_footer(dst).meta
        _require_session_wfz(meta, dst)
    except (ValueError, OSError, zipfile.BadZipFile, KeyError, struct.error) as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise LosslessCheckFailed(f"{dst} is not a complete session .wfz: {exc}") from exc
    here, there = s.meta.get("session_id"), (meta.get("wfacq") or {}).get("session_id")
    if not here or here != there:
        raise SessionRefused(f"{dst} is of session {there!r}, not this folder's {here!r}")
    same_bin = (s.bin_bytes == meta.get("source_bin_bytes")
                and s.fingerprint[BIN][1] == meta.get("source_bin_mtime_ns"))
    if not same_bin:
        raise SessionRefused(f"{BIN} changed since {dst} was made from it: compress it again")
    stored = meta.get("session_files") or {}
    changed = sorted(n for n in set(stored) | set(s.data)
                     if n not in stored or n not in s.data
                     or hashlib.sha256(s.data[n]).hexdigest() != stored[n].get("sha256"))
    if changed:
        raise SessionRefused(f"{', '.join(changed)} changed since {dst} was made: "
                             "compress it again")
    v = verify_session(dst, threads=threads, batch=batch, progress=verify_progress)
    if v["expected_bin_sha256"] != meta["source_bin_sha256"]:  # the file changed under us
        raise LosslessCheckFailed(f"{dst} changed while it was being verified")
    # What compress_session adds after the footer is written, from the file just verified.
    meta["output_bytes"] = v["wfz_bytes"]
    meta["footer_bytes"] = v["footer_bytes"]
    meta["ratio"] = s.bin_bytes / meta["output_bytes"]
    _write_sidecars(dst, meta, v, file_log, finished_after_stop=True)
    return _result(session, dst, meta, v, None, t0)


def _write_sidecars(dst: Path, meta: dict, v: dict, file_log,
                    finished_after_stop: bool = False) -> None:
    """The README, preview and receipt beside a .wfz that ``v`` (verify_session) has proved."""
    meta["byte_identical_verified"] = True
    sidecar.write_readme(dst, meta, file_log=file_log)
    sidecar.write_preview_frame(dst, file_log=file_log)
    extra = {
        "byte_identical": v["byte_identical"],
        "bin_sha256": v["bin_sha256"],
        "session_files_ok": v["session_files_ok"],
        "wfz_sha256": v["wfz_sha256"],
        "wfz_bytes": v["wfz_bytes"],
        "verified_by": "stream",
        "verified_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verify_elapsed_s": v["elapsed_s"],
    }
    if finished_after_stop:
        # Verified by finish_session, not by the run that compressed it.
        extra["finished_after_stop"] = True
    sidecar.write_receipt(dst, meta, extra=extra, file_log=file_log, no_clobber=True)


def _result(session: Path, dst: Path, meta: dict, v: dict, compress_s, t0: float) -> dict:
    return {
        "session": str(session),
        "wfz": str(dst),
        "n_frames": meta["n_frames"],
        "source_bytes": meta["source_bytes"],
        "output_bytes": meta["output_bytes"],
        "ratio": meta["ratio"],
        "compress_s": compress_s,
        "verify_s": v["elapsed_s"],
        "bin_sha256": v["bin_sha256"],
        "wfz_sha256": v["wfz_sha256"],
        "byte_identical": v["byte_identical"],
        "elapsed_s": time.perf_counter() - t0,
    }
