"""The ``.wfz`` container.

Layout::

    magic  b"WFZ1\\0\\0\\0\\0"       8 bytes
    uint64 footer_offset            8 bytes, little-endian
    <JPEG-LS codestreams, concatenated, temporal order>
    <footer: a zip archive>

The footer is an ordinary zip so it can be opened with standard tools. It holds:

    meta.json           geometry, bit shift, frame count, provenance, pixel SHA-256
    index.npy           int64 (n_frames, 3): offset, length, crc32 of each codestream
    tarheaders.bin.zst  every original 512-byte tar header, concatenated
    shells.bin.zst      the non-pixel bytes of each member (one copy if they are all identical)
    trailer.bin         the bytes after the last member in the original archive

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
_HEADERS = "tarheaders.bin.zst"
_SHELLS = "shells.bin.zst"
_TRAILER = "trailer.bin"


@dataclass
class Footer:
    meta: dict
    index: np.ndarray
    tar_headers: bytes
    shells: bytes
    trailer: bytes

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
        z.writestr(_HEADERS, compressor.compress(footer.tar_headers))
        z.writestr(_SHELLS, compressor.compress(footer.shells))
        z.writestr(_TRAILER, footer.trailer)
    blob = buf.getvalue()
    fh.write(blob)
    fh.seek(len(MAGIC))
    fh.write(struct.pack("<Q", footer_offset))
    return len(blob)


def read_footer(path: str | Path, max_blob: int = 1 << 34) -> Footer:
    with open(path, "rb") as fh:
        if fh.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"{path} is not a .wfz file")
        offset = struct.unpack("<Q", fh.read(8))[0]
        fh.seek(offset)
        z = zipfile.ZipFile(io.BytesIO(fh.read()))
    d = zstd.ZstdDecompressor()
    return Footer(
        meta=json.loads(z.read(_META)),
        index=np.load(io.BytesIO(z.read(_INDEX))),
        tar_headers=d.decompress(z.read(_HEADERS), max_output_size=max_blob),
        shells=d.decompress(z.read(_SHELLS), max_output_size=max_blob),
        trailer=z.read(_TRAILER),
    )


def read_meta(path: str | Path) -> dict:
    """Just the metadata, without inflating the header/shell blobs."""
    with open(path, "rb") as fh:
        if fh.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"{path} is not a .wfz file")
        offset = struct.unpack("<Q", fh.read(8))[0]
        fh.seek(offset)
        return json.loads(zipfile.ZipFile(io.BytesIO(fh.read())).read(_META))
