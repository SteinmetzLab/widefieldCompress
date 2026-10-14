"""The ``.wfz`` container.

Layout::

    magic  b"WFZ1\\0\\0\\0\\0"       8 bytes
    uint64 footer_offset            8 bytes, little-endian
    <JPEG-LS codestreams, concatenated, temporal order>
    <footer: a zip archive>

The footer is an ordinary zip so it can be opened with standard tools. It holds:

    meta.json           geometry, bit shift, frame count, provenance, pixel SHA-256
    index.npy           int64 (n_frames, 3): offset, length, crc32 of each codestream
    order.npy           int64 (n_frames,): temporal index -> storage index. Absent when member
                        names carry no frame number. These archives are written in lexicographic
                        name order (frame-0, frame-1, frame-10, ...), so storage position is not
                        acquisition order
    tarheaders.bin.zst  every original 512-byte tar header, concatenated
    shells.bin.zst      the non-pixel bytes of each member (one copy if they are all identical)
    trailer.bin         the bytes after the last member in the original archive

Format version 3 adds a second kind of source, a wfAcq session folder (``source_kind`` is
``"wfacq_bin"``; absent means a tar). Such a file has no tar to rebuild, so the three tar entries
above are absent, ``order.npy`` is the identity, and it holds instead:

    session/<name>.zst  each small session file (wf_meta.json, wf_frames.npy, ...), verbatim
                        before compression, with its size and SHA-256 in meta.json

Tar sources are still written as version 2, so a reader that predates version 3 keeps reading
them, and refuses version 3 files with an upgrade message rather than misreading them.

Putting the index at the end lets the file be written in a single forward pass, and putting the
offset at the front means a reader can jump straight to it.
"""

from __future__ import annotations

import io
import json
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import zstandard as zstd

MAGIC = b"WFZ1\0\0\0\0"
HEADER_LEN = len(MAGIC) + 8
PAYLOAD_START = HEADER_LEN

_META = "meta.json"
_INDEX = "index.npy"
_ORDER = "order.npy"
_HEADERS = "tarheaders.bin.zst"
_SHELLS = "shells.bin.zst"
_TRAILER = "trailer.bin"
_SESSION_PREFIX = "session/"

#: ``meta["source_kind"]`` for a .wfz made from a wfAcq session folder rather than a tar.
WFACQ_BIN = "wfacq_bin"


def is_wfacq(meta: dict) -> bool:
    return meta.get("source_kind") == WFACQ_BIN


@dataclass
class Footer:
    meta: dict
    index: np.ndarray
    tar_headers: bytes
    shells: bytes
    trailer: bytes
    order: np.ndarray | None = None  # temporal index -> storage index
    # Written only. Read back one at a time with read_session_file(), not with the footer.
    session_files: dict[str, bytes] | None = None

    def storage_index(self, temporal_i: int) -> int:
        """Where frame ``temporal_i`` of the recording actually sits in the payload."""
        if self.order is None:
            return temporal_i
        return int(self.order[temporal_i])

    def shell_for(self, i: int) -> bytes:
        if self.meta["shells_uniform"]:
            return self.shells
        n = self.meta["shell_len"]
        return self.shells[i * n : (i + 1) * n]


def write_header(fh) -> None:
    """Reserve the fixed-size prologue; the offset is patched in by :func:`finalise`."""
    fh.write(MAGIC + b"\0" * 8)


def finalise(fh, footer_offset: int, footer: Footer, zstd_level: int = 10) -> int:
    """Append the footer zip and patch the offset. Returns the footer's size in bytes."""
    compressor = zstd.ZstdCompressor(level=zstd_level)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(_META, json.dumps(footer.meta, indent=1, sort_keys=True))
        index_buf = io.BytesIO()
        np.save(index_buf, footer.index)
        z.writestr(_INDEX, index_buf.getvalue())
        if footer.order is not None:
            order_buf = io.BytesIO()
            np.save(order_buf, footer.order)
            z.writestr(_ORDER, order_buf.getvalue())
        if is_wfacq(footer.meta):
            for name, data in (footer.session_files or {}).items():
                z.writestr(f"{_SESSION_PREFIX}{name}.zst", compressor.compress(data))
        else:
            z.writestr(_HEADERS, compressor.compress(footer.tar_headers))
            z.writestr(_SHELLS, compressor.compress(footer.shells))
            z.writestr(_TRAILER, footer.trailer)
    blob = buf.getvalue()
    fh.write(blob)
    fh.seek(len(MAGIC))
    fh.write(struct.pack("<Q", footer_offset))
    return len(blob)


def _open_footer_zip(path: str | Path) -> zipfile.ZipFile:
    with open(path, "rb") as fh:
        if fh.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"{path} is not a .wfz file")
        offset = struct.unpack("<Q", fh.read(8))[0]
        fh.seek(offset)
        return zipfile.ZipFile(io.BytesIO(fh.read()))


def read_footer(path: str | Path, max_blob: int = 1 << 34) -> Footer:
    z = _open_footer_zip(path)
    meta = json.loads(z.read(_META))
    check_version(meta, path)
    d = zstd.ZstdDecompressor()
    order = None
    if _ORDER in z.namelist():
        order = np.load(io.BytesIO(z.read(_ORDER)))
    if is_wfacq(meta):
        # Nothing to rebuild a tar from. Only this kind may lack the tar entries: for a tar source a
        # missing blob must stay an error, since an empty one cannot be told from a corrupt one.
        tar_headers = shells = trailer = b""
    else:
        tar_headers = d.decompress(z.read(_HEADERS), max_output_size=max_blob)
        shells = d.decompress(z.read(_SHELLS), max_output_size=max_blob)
        trailer = z.read(_TRAILER)
    return Footer(
        order=order,
        meta=meta,
        index=np.load(io.BytesIO(z.read(_INDEX))),
        tar_headers=tar_headers,
        shells=shells,
        trailer=trailer,
    )


def read_session_file(path: str | Path, name: str, max_blob: int = 1 << 34) -> bytes:
    """One small session file stored in a version 3 .wfz, exactly as it was on disk."""
    z = _open_footer_zip(path)
    meta = json.loads(z.read(_META))
    check_version(meta, path)
    if name not in meta.get("session_files", {}):
        raise KeyError(f"{path} holds no session file {name!r}")
    return zstd.ZstdDecompressor().decompress(
        z.read(f"{_SESSION_PREFIX}{name}.zst"), max_output_size=max_blob
    )


READER_FORMAT_VERSION = 3


def check_version(meta: dict, path) -> None:
    """Refuse a file written by a newer format than this reader understands."""
    v = meta.get("format_version", 0)
    if v > READER_FORMAT_VERSION:
        raise ValueError(
            f"{path} is format version {v}; this build of wfcompress reads up to "
            f"{READER_FORMAT_VERSION}. Install a newer version: "
            + meta.get("provenance", {}).get("repo", "")
        )


def read_meta(path: str | Path) -> dict:
    """Just the metadata, without inflating the header/shell blobs."""
    meta = json.loads(_open_footer_zip(path).read(_META))
    check_version(meta, path)
    return meta
