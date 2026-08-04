"""Minimal forward-only tar reader.

We do not use :mod:`tarfile` because we need the raw 512-byte header blocks verbatim in order to
rebuild a byte-identical archive, and we need each member's data offset so frames can be read by
seek rather than by streaming the whole archive.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

BLOCK = 512


@dataclass(frozen=True)
class Entry:
    """One tar entry. ``size == 0`` for directories and other non-data entries."""

    header: bytes  # the raw 512-byte block, kept so it can be re-emitted verbatim
    name: str
    size: int
    data_offset: int  # absolute byte offset of the member's payload

    @property
    def padded_size(self) -> int:
        return ((self.size + BLOCK - 1) // BLOCK) * BLOCK

    @property
    def end_offset(self) -> int:
        return self.data_offset + self.padded_size


def walk(fh: BinaryIO) -> Iterator[Entry]:
    """Yield every entry in file order, stopping at the end-of-archive marker."""
    off = 0
    while True:
        fh.seek(off)
        header = fh.read(BLOCK)
        if len(header) < BLOCK or header[:1] == b"\0":
            return
        name = header[:100].rstrip(b"\0").decode("utf-8", "surrogateescape")
        raw_size = header[124:136].rstrip(b"\0 ").decode("ascii", "replace")
        size = int(raw_size or "0", 8)
        entry = Entry(header=header, name=name, size=size, data_offset=off + BLOCK)
        yield entry
        off = entry.end_offset


def read_entries(path: str | Path) -> list[Entry]:
    with open(path, "rb") as fh:
        return list(walk(fh))


def trailing_bytes(path: str | Path, entries: list[Entry]) -> bytes:
    """Everything after the last member: the zero blocks and any blocking-factor padding."""
    if not entries:
        return b""
    with open(path, "rb") as fh:
        fh.seek(entries[-1].end_offset)
        return fh.read()
