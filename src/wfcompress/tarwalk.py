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


def walk(fh: BinaryIO, start: int = 0) -> Iterator[Entry]:
    """Yield every entry in file order from ``start``, stopping at the end-of-archive marker."""
    off = start
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
            # The constant stride stops holding here. Everything before this point sits at a
            # computed offset *and* passed its own checksum and size check, so the chain up to
            # here is sound and can be kept; only the remainder needs the slow walk.
            return _walk_from(path, entries)
        entries.append(e)

    # Anything after the last entry we accounted for means the stride ran out before the archive
    # did - a mixed archive whose trailing content happens to start on a stride boundary. Resume
    # the sequential walk there rather than restarting it.
    with open(path, "rb") as fh:
        after = _entry_at(fh, entries[-1].end_offset)
    if after is not None:
        return _walk_from(path, entries)
    return entries


def _walk_from(path: Path, prefix: list[Entry]) -> list[Entry]:
    """Keep a validated prefix and walk the rest sequentially.

    Some ``widefield.tar`` files hold a run of equally-sized frames and then a whole SpikeGLX
    recording. Restarting the walk from offset 0 costs one network round trip per member - 405
    seconds on a 344 GB archive - to re-derive a prefix that was already proven correct. Resuming
    from the break reduces that to the handful of members that actually follow it.
    """
    entries = list(prefix)
    start = entries[-1].end_offset if entries else 0
    with open(path, "rb") as fh:
        for e in walk(fh, start):
            # `walk` stops at a zero block and raises on an unparseable size, but it will happily
            # parse arbitrary bytes that merely start non-zero. Past the point where the stride
            # broke we have no other guarantee that we are looking at headers at all, so check.
            if not header_checksum_ok(e.header):
                raise MalformedArchive(
                    f"header checksum fails at offset {e.data_offset - BLOCK:,}; "
                    f"the archive is not a plain sequence of tar members from there on"
                )
            entries.append(e)
    return entries


def uniform_prefix(path: str | Path) -> tuple[Entry | None, int, int]:
    """Find the leading run of equally-sized members without enumerating it.

    Returns ``(first_data_entry, n_uniform_members, offset_just_past_them)``.

    Reading every header to answer "where do the frames stop?" costs one network round trip per
    member - 226,159 of them on one of these archives, minutes over a busy link. But the members
    are equally sized, so header k sits at a computable offset and validity is monotonic: bisecting
    for the last valid one answers the same question in about twenty reads.

    ``n_uniform_members`` counts data members only; a leading directory entry is accounted for in
    the returned offset but not the count.
    """
    path = Path(path)
    total = path.stat().st_size
    with open(path, "rb") as fh:
        first = _entry_at(fh, 0)
        if first is None:
            return None, 0, 0
        probe, off = first, 0
        while probe is not None and probe.size == 0:
            off = probe.end_offset
            probe = _entry_at(fh, off)
        if probe is None:
            return None, 0, off

        stride = BLOCK + probe.padded_size
        start = probe.data_offset - BLOCK
        n = (total - start) // stride

        def ok(k: int) -> bool:
            e = _entry_at(fh, start + k * stride)
            return e is not None and e.size == probe.size and header_checksum_ok(e.header)

        if n >= 1 and ok(n - 1):
            return probe, n, start + n * stride
        lo, hi = 0, n - 1                       # ok(lo) true, ok(hi) false
        if not ok(0):
            return probe, 0, start
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if ok(mid):
                lo = mid
            else:
                hi = mid
        return probe, lo + 1, start + (lo + 1) * stride


def entries_after(path: str | Path, offset: int) -> list[Entry]:
    """Sequentially walk whatever follows ``offset``. Cheap when it is a handful of members."""
    out: list[Entry] = []
    with open(path, "rb") as fh:
        for e in walk(fh, offset):
            if not header_checksum_ok(e.header):
                raise MalformedArchive(
                    f"header checksum fails at offset {e.data_offset - BLOCK:,}"
                )
            out.append(e)
    return out


def trailing_bytes(path: str | Path, entries: list[Entry]) -> bytes:
    """Everything after the last member: the zero blocks and any blocking-factor padding."""
    if not entries:
        return b""
    with open(path, "rb") as fh:
        fh.seek(entries[-1].end_offset)
        return fh.read()
