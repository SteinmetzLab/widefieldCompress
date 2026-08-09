"""Does the size of each write matter when writing a .wfz onto the share?

`compress` writes one JPEG-LS codestream at a time - roughly 250-350 kB per frame - straight to
the output file. Writing the same file as a block copy afterwards is measurably faster, which
suggests the per-frame writes are the difference rather than anything about the network.

This isolates that: same total bytes, same destination, only the write size changes. Pure I/O, so
it stays meaningful even with the bulk job running (both arms share the link equally).
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

SERVER = Path(r"Y:\temp\wfChunkTest")
LOCAL = Path(r"D:\temp\wfChunkTest")


def write_in_chunks(dst: Path, total: int, chunk: int, payload: bytes) -> float:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    written = 0
    t0 = time.perf_counter()
    with open(dst, "wb") as fh:
        while written < total:
            n = min(chunk, total - written)
            fh.write(payload[:n])
            written += n
        fh.flush()
        os.fsync(fh.fileno())
    dt = time.perf_counter() - t0
    dst.unlink(missing_ok=True)
    return dt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mb", type=int, default=768, help="bytes to write per trial")
    ap.add_argument("--local", action="store_true", help="also measure the local disk")
    args = ap.parse_args()

    total = args.mb * 1024 * 1024
    payload = os.urandom(16 << 20)  # incompressible, so nothing dedupes it away
    sizes = [64 << 10, 256 << 10, 1 << 20, 4 << 20, 16 << 20]

    for label, root in (("share", SERVER),) + ((("local", LOCAL),) if args.local else ()):
        print(f"\n{label}: {args.mb} MB per trial")
        for chunk in sizes:
            dt = write_in_chunks(root / "chunk.bin", total, chunk, payload)
            note = ""
            if chunk == (256 << 10):
                note = "   <- roughly one JPEG-LS frame"
            elif chunk == (4 << 20):
                note = "   <- what a buffered writer would do"
            print(f"  {chunk/1024:>8,.0f} kB writes   {dt:6.2f} s   "
                  f"{total/1e6/dt:6.0f} MB/s{note}")
        try:
            root.rmdir()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
