"""Does buffering the .wfz output recover the gap between staging locally and writing direct?

Writing a finished .wfz to the share as a block copy runs several times faster than writing the
same bytes incrementally during compression, because `compress` emits one JPEG-LS codestream at a
time and SMB throughput scales hard with write size (see bench_smb_chunk_size.py).

If that is the whole story, giving the output file a large buffer should make writing straight to
the share as fast as writing locally - and make staging pointless.

Runs the same archive through both settings back to back, so a busy machine biases both equally.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from wfcompress import codec

LOCAL = Path(r"D:\temp\wfBuf")
SERVER = Path(r"Y:\temp\wfBuf")


def timed_copy(src: Path, dst: Path, block: int = 1 << 24) -> float:
    dst.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    with open(src, "rb") as fi, open(dst, "wb", buffering=block) as fo:
        while chunk := fi.read(block):
            fo.write(chunk)
        fo.flush()
        os.fsync(fo.fileno())
    return time.perf_counter() - t0


def run(tar: Path, dst: Path, buffering: int, threads: int, shape) -> tuple[float, int]:
    dst.unlink(missing_ok=True)
    saved = codec.WRITE_BUFFER
    codec.WRITE_BUFFER = buffering
    try:
        t0 = time.perf_counter()
        meta = codec.compress(tar, dst, threads=threads, shape=shape)
        return time.perf_counter() - t0, meta["output_bytes"]
    finally:
        codec.WRITE_BUFFER = saved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tar", default=str(LOCAL / "tiff.tar"))
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--shape", type=int, nargs=2, default=None)
    args = ap.parse_args()

    tar = Path(args.tar)
    n = tar.stat().st_size
    shape = tuple(args.shape) if args.shape else None
    SERVER.mkdir(parents=True, exist_ok=True)
    print(f"{tar.name}  {n/1e9:.2f} GB, {args.threads} threads\n")

    rows = []
    for label, dst, buf in (
        ("local,  default 8 kB buffer", LOCAL / "a.wfz", 8192),
        ("local,  16 MB buffer", LOCAL / "b.wfz", 16 << 20),
        ("share,  default 8 kB buffer", SERVER / "c.wfz", 8192),
        ("share,  16 MB buffer", SERVER / "d.wfz", 16 << 20),
    ):
        dt, out = run(tar, dst, buf, args.threads, shape)
        rows.append((label, dt, out))
        print(f"  {label:<30} {dt:7.1f} s  {n/1e6/dt:6.0f} MB/s")

    wfz = LOCAL / "b.wfz"
    dtc = timed_copy(wfz, SERVER / "copied.wfz")
    print(f"  {'copy the finished .wfz across':<30} {dtc:7.1f} s  "
          f"{wfz.stat().st_size/1e6/dtc:6.0f} MB/s")

    staged = rows[1][1] + dtc
    direct = rows[3][1]
    print(f"\n  stage locally then copy : {rows[1][1]:.1f} + {dtc:.1f} = {staged:.1f} s")
    print(f"  write straight to share : {direct:.1f} s")
    verdict = "staging still wins" if staged < direct * 0.95 else "no meaningful difference"
    print(f"  -> {verdict}")

    for p in (LOCAL / "a.wfz", LOCAL / "b.wfz", SERVER / "c.wfz", SERVER / "d.wfz",
              SERVER / "copied.wfz"):
        p.unlink(missing_ok=True)
    try:
        SERVER.rmdir()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
