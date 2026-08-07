"""The current pipeline's real rate: 7-Zip building a tar of loose TIFFs straight onto the share.

The 7-Zip figure in bench_loose_tiff_source.py is measured *after* three other passes have already
walked every file, so the page cache and Windows Defender's scan cache are both fully warm. This
does the same measurement as the first thing that touches the freshly written frames, which is
closer to what happens on the acquisition machine.

It is still not properly cold - the extraction just wrote these bytes, so they sit in the write
cache. A real session is 100-300 GB against 64 GB of RAM, so treat any number here as an upper
bound on the current route.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import time
from pathlib import Path

SEVENZIP = r"C:\Program Files\7-Zip\7z.exe"
LOCAL = Path(r"D:\temp\wfBench")
SERVER = Path(r"Y:\temp\wfBench")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wfz", default=r"Y:\Subjects\test\2026-02-17\1\widefield.wfz")
    ap.add_argument("--tag", default="cold")
    args = ap.parse_args()

    from wfcompress import extract

    tifs = LOCAL / f"tifs_{args.tag}"
    shutil.rmtree(tifs, ignore_errors=True)
    print(f"extracting {args.wfz} -> {tifs}", flush=True)
    r = extract(args.wfz, tifs, fmt="files", overwrite=True)
    nbytes = r["bytes_written"]
    print(f"  {r['n_frames']:,} files, {nbytes/1e9:.2f} GB\n", flush=True)

    SERVER.mkdir(parents=True, exist_ok=True)
    dst = SERVER / f"{args.tag}.tar"
    dst.unlink(missing_ok=True)

    t0 = time.perf_counter()
    p = subprocess.run([SEVENZIP, "a", "-ttar", "-bso0", "-bsp0", str(dst), "."],
                       cwd=tifs, capture_output=True, check=False)
    dt = time.perf_counter() - t0
    if p.returncode != 0:
        print(f"7z failed: {p.stderr[:300]!r}")
        return 1

    made = dst.stat().st_size
    print("7-Zip: loose TIFFs -> tar on the share")
    print(f"  {dt:.1f} s for {nbytes/1e9:.2f} GB  =  {nbytes/1e6/dt:.0f} MB/s")
    print(f"  tar is {made/1e9:.2f} GB")
    print(f"\n  per TB of raw frames: {dt/nbytes*1e12/3600:.2f} h to clear the local disk")
    print("\n  compare with the JPEG-LS encode rate (52 MB/s to the share, 65 MB/s local,")
    print("  16 threads) from bench_acquisition_transfer.py.")

    dst.unlink(missing_ok=True)
    try:
        SERVER.rmdir()
    except OSError:
        pass
    shutil.rmtree(tifs, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
