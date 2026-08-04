"""Is compression limited by SMB or by our own code? Compress the same file local and over the wire.

Also sweeps thread count, because JPEG-LS releases the GIL but the numpy glue around it does not.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from wfcompress import codec

NET_SRC = Path(r"Y:\temp\wfCompressTest\widefield.ORIGINAL.tar")
LOCAL_DIR = Path(r"D:\temp\wfc-bench")
LOCAL_DIR.mkdir(parents=True, exist_ok=True)
LOCAL_SRC = LOCAL_DIR / "widefield.tar"

if not LOCAL_SRC.exists():
    t0 = time.perf_counter()
    shutil.copy2(NET_SRC, LOCAL_SRC)
    dt = time.perf_counter() - t0
    n = LOCAL_SRC.stat().st_size
    print(f"copied {n/1e9:.2f} GB off the share in {dt:.0f} s = {n/1e6/dt:.0f} MB/s "
          f"(plain sequential read, no processing)")

n = LOCAL_SRC.stat().st_size
print(f"\nsource {n/1e9:.2f} GB\n")
print(f"{'variant':<34s}{'threads':>8s}{'min':>7s}{'MB/s':>8s}{'ratio':>7s}")
print("-" * 64)


def run(label, src, dst, threads):
    if dst.exists():
        dst.unlink()
    t0 = time.perf_counter()
    meta = codec.compress(src, dst, threads=threads)
    dt = time.perf_counter() - t0
    print(f"{label:<34s}{threads:>8d}{dt/60:>7.2f}{n/1e6/dt:>8.1f}{meta['ratio']:>7.2f}")
    if dst.exists():
        dst.unlink()
    for extra in (dst.with_name(dst.name + ".README.md"),
                  dst.with_name(dst.name + ".receipt.json")):
        if extra.exists():
            extra.unlink()
    return dt


for t in (4, 8, 16, 24):
    run("local disk -> local disk", LOCAL_SRC, LOCAL_DIR / "out.wfz", t)

run("share -> share", NET_SRC, Path(r"Y:\temp\wfCompressTest\bench.wfz"), 8)
run("share -> local disk", NET_SRC, LOCAL_DIR / "out.wfz", 8)
run("local disk -> share", LOCAL_SRC, Path(r"Y:\temp\wfCompressTest\bench.wfz"), 8)
