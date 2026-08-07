"""How fast can the acquisition machine read a session's loose TIFFs at all?

The first benchmark compares writing an existing tar to the share against compressing it. That
flatters the current pipeline, because `my7zTar` does not read one big sequential file - it reads
a few hundred thousand individual TIFFs off the local disk, and per-file overhead is real.

That read cost is common to both routes, so it sets a floor on *both*. If reading loose frames is
already slower than JPEG-LS can encode them, compressing on the way out is free.

Measures, on a real session's frames extracted to local disk:
  1. read every TIFF sequentially, doing nothing with the bytes
  2. tar them straight onto the share, which is what the pipeline does today
  3. tar them to local disk, to separate the network from the per-file overhead

Writes go to a scratch directory and to the server's `temp` share; both are cleaned up.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

SEVENZIP = r"C:\Program Files\7-Zip\7z.exe"

LOCAL = Path(r"D:\temp\wfBench")
TIFS = LOCAL / "tifs"
SERVER = Path(r"Y:\temp\wfBench")


def read_all(files: list[Path], block: int = 1 << 20) -> tuple[float, int]:
    t0 = time.perf_counter()
    total = 0
    for f in files:
        with open(f, "rb") as fh:
            while chunk := fh.read(block):
                total += len(chunk)
    return time.perf_counter() - t0, total


def tar_to(files: list[Path], root: Path, dst: Path) -> float:
    """Reference only - **do not quote this number for the share**.

    Python's ``tarfile`` copies members through ``copyfileobj`` in 16 kB chunks, so writing to SMB
    costs a round trip per 16 kB. Trying to fix that by wrapping the output in a large
    ``BufferedWriter`` and using stream mode is worse, not better: ``_Stream.write`` does
    ``self.buf += s`` then re-slices, which is quadratic in the buffer size - a 4 MB buffer took
    76 s locally where the plain version took 3.2 s.

    The pipeline calls 7-Zip, so :func:`tar_to_external` is the number that means anything.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    with tarfile.open(dst, "w", format=tarfile.GNU_FORMAT) as tf:
        for f in files:
            tf.add(f, arcname=str(f.relative_to(root)))
    return time.perf_counter() - t0


def tar_to_external(exe: str, args: list[str], cwd: Path, dst: Path) -> float | None:
    """Time an external archiver, so the comparison is against the tool the pipeline uses."""
    if not Path(exe).exists():
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    t0 = time.perf_counter()
    r = subprocess.run([exe, *args], cwd=cwd, capture_output=True, check=False)
    if r.returncode != 0:
        print(f"    ({Path(exe).name} failed: {r.stderr[:200]!r})")
        return None
    return time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wfz", default=r"Y:\Subjects\test\2026-02-17\1\widefield.wfz")
    ap.add_argument("--keep", action="store_true", help="leave the extracted TIFFs in place")
    args = ap.parse_args()

    if not TIFS.exists() or not any(TIFS.rglob("*")):
        from wfcompress import extract

        print(f"extracting frames from {args.wfz} ...", flush=True)
        t0 = time.perf_counter()
        r = extract(args.wfz, TIFS, fmt="files", overwrite=True)
        print(f"  {r['n_frames']:,} files, {r['bytes_written']/1e9:.2f} GB in "
              f"{time.perf_counter()-t0:.1f} s")

    files = sorted(p for p in TIFS.rglob("*") if p.is_file())
    nbytes = sum(f.stat().st_size for f in files)
    print(f"\n{len(files):,} loose frames, {nbytes/1e9:.2f} GB, "
          f"{nbytes/len(files)/1e3:.0f} kB each\n")

    dt, got = read_all(files)
    print(f"  read every file, discard   {dt:7.1f} s  {got/1e6/dt:6.0f} MB/s   "
          f"({len(files)/dt:,.0f} files/s)")
    floor = got / 1e6 / dt

    dt = tar_to(files, TIFS, LOCAL / "loose_local.tar")
    print(f"  tar -> local disk          {dt:7.1f} s  {nbytes/1e6/dt:6.0f} MB/s")
    (LOCAL / "loose_local.tar").unlink(missing_ok=True)

    SERVER.mkdir(parents=True, exist_ok=True)
    dt = tar_to(files, TIFS, SERVER / "loose_server.tar")
    print(f"  tar -> the share (python)  {dt:7.1f} s  {nbytes/1e6/dt:6.0f} MB/s")
    current = nbytes / 1e6 / dt
    (SERVER / "loose_server.tar").unlink(missing_ok=True)

    # the pipeline calls my7zTar, so measure the real tool as well
    dst = SERVER / "loose_7z.tar"
    dt7 = tar_to_external(SEVENZIP, ["a", "-ttar", "-bso0", "-bsp0", str(dst), "."], TIFS, dst)
    if dt7:
        print(f"  tar -> the share (7-Zip)   {dt7:7.1f} s  {nbytes/1e6/dt7:6.0f} MB/s   "
              f"<- what the pipeline does today")
        current = max(current, nbytes / 1e6 / dt7)
        dst.unlink(missing_ok=True)

    dst = SERVER / "loose_bsdtar.tar"
    dtb = tar_to_external(r"C:\Windows\system32\tar.exe", ["-cf", str(dst), "."], TIFS, dst)
    if dtb:
        print(f"  tar -> the share (bsdtar)  {dtb:7.1f} s  {nbytes/1e6/dtb:6.0f} MB/s")
        current = max(current, nbytes / 1e6 / dtb)
        dst.unlink(missing_ok=True)

    try:
        SERVER.rmdir()
    except OSError:
        pass

    print(f"\n  reading the frames alone caps any route at {floor:.0f} MB/s.")
    print(f"  the current route achieves {current:.0f} MB/s end to end.")
    print("  compare the JPEG-LS encode rate from bench_acquisition_transfer.py against "
          f"{floor:.0f} MB/s:")
    print("    encode faster than that -> compressing on the way out is free")
    print("    encode slower           -> the difference is the extra time to clear the disk")

    if not args.keep:
        shutil.rmtree(TIFS, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
