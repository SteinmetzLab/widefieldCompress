"""Minimal forward-only tar reader.

We do not use :mod:`tarfile` because we need the raw 512-byte header blocks verbatim in order to
rebuild a byte-identical archive, and we need each member's data offset so frames can be read by
seek rather than by streaming the whole archive.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
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


class MalformedArchive(ValueError):
    """A tar header could not be parsed. Carries the offset, which a bare ValueError did not."""


def parse_size(field: bytes) -> int:
    """Decode a tar header's size field.

    Normally octal ASCII, but GNU tar switches to base-256 binary (high bit of the first byte set)
    for values that will not fit - in practice, members of 8 GiB or more. Some of these archives
    bundle a whole session, widefield frames plus the SpikeGLX recording, and an imec ``.bin`` is
    comfortably over that, so the encoding is not exotic here.
    """
    if field and field[0] & 0x80:
        return int.from_bytes(bytes([field[0] & 0x7F]) + field[1:], "big")
    text = field.rstrip(b"\0 ").decode("ascii", "replace")
    try:
        return int(text or "0", 8)
    except ValueError as e:
        raise MalformedArchive(f"size field is neither octal nor base-256: {field!r}") from e


def walk(fh: BinaryIO) -> Iterator[Entry]:
    """Yield every entry in file order, stopping at the end-of-archive marker."""
    off = 0
    while True:
        fh.seek(off)
        header = fh.read(BLOCK)
        if len(header) < BLOCK or header[:1] == b"\0":
            return
        name = header[:100].rstrip(b"\0").decode("utf-8", "surrogateescape")
        try:
            size = parse_size(header[124:136])
        except MalformedArchive as e:
            raise MalformedArchive(f"{e} at offset {off:,}") from None
        entry = Entry(header=header, name=name, size=size, data_offset=off + BLOCK)
        yield entry
        off = entry.end_offset


def header_checksum_ok(header: bytes) -> bool:
    """Validate a tar header against its own checksum field.

    Used to confirm that a header found at a *computed* offset really is a header, rather than
    pixel data that happens to sit there.
    """
    try:
        stored = int(header[148:156].rstrip(b"\0 ").decode("ascii") or "-1", 8)
    except ValueError:
        return False
    blanked = header[:148] + b" " * 8 + header[156:]
    return stored in (sum(blanked), sum(bytearray(blanked)))


def _entry_at(fh: BinaryIO, offset: int) -> Entry | None:
    fh.seek(offset)
    header = fh.read(BLOCK)
    if len(header) < BLOCK or header[:1] == b"\0":
        return None
    name = header[:100].rstrip(b"\0").decode("utf-8", "surrogateescape")
    try:
        size = parse_size(header[124:136])
    except MalformedArchive:
        return None
    return Entry(header=header, name=name, size=size, data_offset=offset + BLOCK)


def read_entries(path: str | Path, workers: int = 16) -> list[Entry]:
    """Every entry in file order.

    The obvious implementation - walk forward, one seek and one 512-byte read per member - costs a
    separate network round trip per member. Measured on a 344 GB archive over SMB that is 544,035
    round trips at 0.744 ms each: **405 seconds**, before a single frame is encoded, and far worse
    with eight workers competing for the link.

    These archives hold one equally-sized frame per member, so once the first member's size is
    known every subsequent header sits at a computable offset and they can all be fetched
    concurrently. Each header is validated against its own checksum and expected size; anything
    unexpected falls back to the sequential walk, which is always correct if slow.
    """
    path = Path(path)
    total = path.stat().st_size

    with open(path, "rb") as fh:
        first = _entry_at(fh, 0)
        if first is None:
            return []
        # a leading directory entry is common; the stride comes from the first *data* member
        lead = [first]
        probe = first
        while probe is not None and probe.size == 0:
            probe = _entry_at(fh, probe.end_offset)
            if probe is not None:
                lead.append(probe)
        if probe is None:
            return [e for e in lead if e is not None]

        stride = BLOCK + probe.padded_size
        start = probe.data_offset - BLOCK
        n = (total - start) // stride
        if n <= 1:
            with open(path, "rb") as f2:
                return list(walk(f2))

        offsets = [start + k * stride for k in range(n)]

    def fetch(chunk: list[int]) -> list[Entry | None]:
        out = []
        with open(path, "rb") as f2:
            for off in chunk:
                out.append(_entry_at(f2, off))
        return out

    step = max(1, len(offsets) // (workers * 4))
    chunks = [offsets[i : i + step] for i in range(0, len(offsets), step)]
    fetched: list[Entry | None] = []
    with ThreadPoolExecutor(workers) as pool:
        for part in pool.map(fetch, chunks):
            fetched.extend(part)

    entries = lead[:-1] + []
    for e in fetched:
        if e is None:  # end of archive reached early; the rest is padding
            break
        if e.size != probe.size or not header_checksum_ok(e.header):
            # the constant-stride assumption does not hold for this archive
            with open(path, "rb") as f2:
                return list(walk(f2))
        entries.append(e)

    # confirm nothing follows the last entry we accounted for, or fall back
    with open(path, "rb") as fh:
        after = _entry_at(fh, entries[-1].end_offset)
    if after is not None:
        with open(path, "rb") as f2:
            return list(walk(f2))
    return entries


def trailing_bytes(path: str | Path, entries: list[Entry]) -> bytes:
    """Everything after the last member: the zero blocks and any blocking-factor padding."""
    if not entries:
        return b""
    with open(path, "rb") as fh:
        fh.seek(entries[-1].end_offset)
        return fh.read()
