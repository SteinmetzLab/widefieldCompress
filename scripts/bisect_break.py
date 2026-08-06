"""Binary-search for the first computed member offset that is not a valid tar header.

The archive's arithmetic is self-consistent (size = 512 + N*stride + trailer), yet a header near
the end is garbage, so something in the middle must break the constant stride. O(log N) reads
finds it without walking 373,794 members over a contended link.
"""

from __future__ import annotations

import sys
from pathlib import Path

BLOCK = 512
TARGETS = [
    r"Y:\Subjects\FD_011\2026-02-25\1\widefield.tar",
    r"Y:\Subjects\AL_0038\2025-05-02\1\widefield.tar",
]


def valid_at(fh, off: int) -> tuple[bool, str]:
    fh.seek(off)
    h = fh.read(BLOCK)
    if len(h) < BLOCK:
        return False, "short read"
    if not h.strip(b"\0"):
        return False, "zero block"
    if h[257:263] not in (b"ustar\x00", b"ustar "):
        return False, f"bad magic {h[257:263]!r}"
    field = h[124:136]
    try:
        size = int(field.rstrip(b"\0 ").decode("ascii") or "0", 8)
    except ValueError:
        return False, f"non-octal size {field!r}"
    name = h[:100].rstrip(b"\0").decode("ascii", "backslashreplace")
    return True, f"{name[:58]} size={size:,}"


for t in sys.argv[1:] or TARGETS:
    p = Path(t)
    total = p.stat().st_size
    print(f"\n{'=' * 74}\n{p.parent}\n  {total:,} bytes")

    with open(p, "rb") as fh:
        fh.seek(0)
        h0 = fh.read(BLOCK)
        lead = BLOCK if int(h0[124:136].rstrip(b"\0 ").decode() or "0", 8) == 0 else 0
        fh.seek(lead)
        h1 = fh.read(BLOCK)
        msize = int(h1[124:136].rstrip(b"\0 ").decode() or "0", 8)
        stride = BLOCK + ((msize + BLOCK - 1) // BLOCK) * BLOCK
        n = (total - lead) // stride
        print(f"  member {msize:,} B, stride {stride:,}, {n:,} members implied, "
              f"{total - lead - n*stride:,} bytes left over")

        def ok(k):
            return valid_at(fh, lead + k * stride)[0]

        if ok(n - 1):
            print("  the last computed member IS valid - nothing to bisect")
            continue
        lo, hi = 0, n - 1                      # ok(lo) true, ok(hi) false
        if not ok(0):
            print("  even member 0 is invalid")
            continue
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if ok(mid):
                lo = mid
            else:
                hi = mid
        good, desc = valid_at(fh, lead + lo * stride)
        bad, why = valid_at(fh, lead + hi * stride)
        print(f"  last valid member : #{lo:,} at offset {lead + lo*stride:,}")
        print(f"      {desc}")
        print(f"  first bad member  : #{hi:,} at offset {lead + hi*stride:,}  ({why})")
        print(f"  that is {100*(lead + hi*stride)/total:.2f}% through the file, "
              f"{total - (lead + hi*stride):,} bytes from the end")
        fh.seek(lead + hi * stride)
        print(f"  bytes there: {fh.read(48)!r}")
