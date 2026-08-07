"""Would writing straight to .wfz clear the acquisition machine's disk as fast as writing a tar?

The pipeline's constraint is not total work, it is *time until the local disk is free for the next
recording*. Both routes read the same loose TIFFs off the local disk, so that cost is common and
cancels. What differs is what happens next:

    current   write 100% of the bytes over the network
    proposed  JPEG-LS encode, then write ~42% of the bytes over the network

So the comparison is ``net_time`` versus ``max(encode_time, 0.42 * net_time)`` if the two overlap,
or their sum if they do not. Everything here is measured rather than assumed.

Run it on an idle machine - a concurrent bulk job makes the encode numbers meaningless.
Writes go to a scratch directory locally and to the server's `temp` share, and are cleaned up.
"""

from __future__ import annotations

import argparse
import os
import shutil
import time
from pathlib import Path

from wfcompress import codec

LOCAL = Path(r"D:\temp\wfBench")
SERVER = Path(r"Y:\temp\wfBench")


def timed_copy(src: Path, dst: Path, block: int = 1 << 22) -> float:
    """Copy and return seconds, forcing the data out to the device before stopping the clock."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while chunk := fi.read(block):
            fo.write(chunk)
        fo.flush()
        os.fsync(fo.fileno())
    return time.perf_counter() - t0


def rate(nbytes: int, seconds: float) -> str:
    return f"{nbytes/1e6/seconds:6.0f} MB/s"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threads", type=int, nargs="+", default=[4, 8, 16])
    ap.add_argument("--skip-server", action="store_true")
    ap.add_argument("--shape", type=int, nargs=2, default=[560, 560],
                    help="geometry for headerless archives, which carry none")
    args = ap.parse_args()
    shape = tuple(args.shape)

    tars = sorted(LOCAL.glob("*.tar"))
    if not tars:
        raise SystemExit(f"no .tar files in {LOCAL}; copy a couple of real archives there first")

    print(f"{os.cpu_count()} logical cores\n")
    results = {}

    for tar in tars:
        n = tar.stat().st_size
        print(f"{'=' * 74}\n{tar.name}  {n/1e9:.2f} GB")

        # --- the current pipeline's network step: push the whole uncompressed archive ---
        if not args.skip_server:
            SERVER.mkdir(parents=True, exist_ok=True)
            dt = timed_copy(tar, SERVER / tar.name)
            print(f"  tar  -> server        {dt:7.1f} s  {rate(n, dt)}   (current pipeline)")
            results[(tar.name, "tar_to_server")] = (dt, n)
            (SERVER / tar.name).unlink()

        # --- encode rate, local -> local, so neither network end is involved ---
        for th in args.threads:
            out = LOCAL / f"{tar.stem}_t{th}.wfz"
            out.unlink(missing_ok=True)
            t0 = time.perf_counter()
            meta = codec.compress(tar, out, threads=th, shape=shape)
            dt = time.perf_counter() - t0
            print(f"  wfz  local, {th:2d} thr    {dt:7.1f} s  {rate(n, dt)}   "
                  f"x{meta['ratio']:.2f} -> {meta['output_bytes']/1e9:.2f} GB")
            results[(tar.name, f"wfz_local_t{th}")] = (dt, n)
            if th != args.threads[-1]:
                out.unlink(missing_ok=True)

        best = max(args.threads)
        wfz = LOCAL / f"{tar.stem}_t{best}.wfz"
        wn = wfz.stat().st_size

        # --- the proposed pipeline: encode straight onto the share ---
        if not args.skip_server:
            out = SERVER / f"{tar.stem}.wfz"
            out.unlink(missing_ok=True)
            t0 = time.perf_counter()
            codec.compress(tar, out, threads=best, shape=shape)
            dt = time.perf_counter() - t0
            print(f"  wfz  -> server, {best} thr {dt:7.1f} s  {rate(n, dt)}   (proposed pipeline)")
            results[(tar.name, "wfz_to_server")] = (dt, n)

            # --- and what inline verification would add on top ---
            t0 = time.perf_counter()
            codec.verify(out, threads=best)
            dv = time.perf_counter() - t0
            print(f"  + streaming verify    {dv:7.1f} s  {rate(n, dv)}   "
                  f"(replaces the size check with a real proof)")
            results[(tar.name, "verify")] = (dv, n)
            out.unlink(missing_ok=True)

            # --- for reference: how fast can the link take the compressed file alone ---
            dt = timed_copy(wfz, SERVER / wfz.name)
            print(f"  wfz  -> server (copy) {dt:7.1f} s  {rate(wn, dt)} of {wn/1e9:.2f} GB")
            results[(tar.name, "wfz_copy")] = (dt, wn)
            (SERVER / wfz.name).unlink()

        wfz.unlink(missing_ok=True)

    # --- the answer ---
    print(f"\n{'=' * 74}\nTIME TO CLEAR THE LOCAL DISK, per TB of raw frames\n")
    print(f"  {'archive':<12} {'current (tar)':>16} {'proposed (wfz)':>16} {'ratio':>8}")
    for tar in tars:
        k = tar.name
        if (k, "tar_to_server") not in results:
            continue
        dt_tar, n = results[(k, "tar_to_server")]
        dt_wfz, _ = results[(k, "wfz_to_server")]
        h_tar = dt_tar / n * 1e12 / 3600
        h_wfz = dt_wfz / n * 1e12 / 3600
        print(f"  {k:<12} {h_tar:13.2f} h {h_wfz:14.2f} h {h_wfz/h_tar:7.1f}x")

    try:
        shutil.rmtree(SERVER)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
