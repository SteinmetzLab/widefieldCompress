"""Locate where two archives stop being well-formed tars.

Both failed with 'invalid literal for int() with base 8' while parsing a header size field. Walk
them defensively and report the first offset where the structure breaks, plus what is actually
there.
"""

from __future__ import annotations

import sys
from pathlib import Path

BLOCK = 512
TARGETS = [
    r"Y:\Subjects\FD_011\2026-02-25\1\widefield.tar",
    r"Y:\Subjects\AL_0038\2025-05-02\1\widefield.tar",
]


def safe(b: bytes, n: int = 60) -> str:
    return b[:n].decode("ascii", "backslashreplace").rstrip("\\x00")


def classify(h: bytes) -> tuple[str, int | None]:
    if len(h) < BLOCK:
        return "short read (truncated file)", None
    if h[:1] == b"\0" and not h.strip(b"\0"):
        return "zero block (end-of-archive marker)", None
    if h[257:263] not in (b"ustar\x00", b"ustar "):
        return f"not a tar header, magic={safe(h[257:263], 8)!r}", None
    field = h[124:136]
    if field[0] & 0x80:
        return "GNU base-256 size field", int.from_bytes(field[1:], "big")
    try:
        return "ok", int(field.rstrip(b"\0 ").decode("ascii") or "0", 8)
    except ValueError:
        return f"size field not octal: {field!r}", None


for t in sys.argv[1:] or TARGETS:
    p = Path(t)
    total = p.stat().st_size
    print(f"\n{'=' * 74}\n{p}")
    print(f"  {total:,} bytes ({total/1e9:.2f} GB)")

    off = n = 0
    with open(p, "rb") as fh:
        while True:
            fh.seek(off)
            h = fh.read(BLOCK)
            kind, size = classify(h)
            if kind != "ok":
                print(f"  walked {n:,} entries cleanly, then at offset {off:,} "
                      f"({100*off/total:.2f}% through): {kind}")
                print(f"    remaining after this point: {total-off:,} bytes")
                if len(h) >= 16:
                    print(f"    first 32 bytes there: {h[:32]!r}")
                break
            if n < 2:
                print(f"  [{n}] {safe(h[:100])!r} size={size:,}")
            off += BLOCK + ((size + BLOCK - 1) // BLOCK) * BLOCK
            n += 1
            if off >= total:
                print(f"  walked {n:,} entries and reached EOF at {off:,} "
                      f"(file is {total:,}) - no end-of-archive marker")
                break

    # a complete tar ends with at least two zero blocks
    with open(p, "rb") as fh:
        fh.seek(max(0, total - 4096))
        tail = fh.read()
    print(f"  last 4 KB: {'all zero' if not tail.strip(chr(0).encode()) else 'NOT all zero'}")
    nz = len(tail.rstrip(b"\0"))
    print(f"  trailing zero bytes at EOF: {len(tail)-nz:,}")
