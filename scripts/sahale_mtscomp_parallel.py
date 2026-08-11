"""Does mtscomp scale better across processes than across threads? Run this ON sahale.

The thread sweep showed heavy diminishing returns - 8 to 32 threads, a fourfold increase, bought
only 1.57x (29.7 -> 46.6 MB/s). Two things explain that, and both point the same way:

  * mtscomp processes a *batch* of n_threads chunks and joins before starting the next, so every
    batch waits on its slowest chunk;
  * the verify pass took a flat ~29 s in all three runs regardless of thread count, so it looks
    single-threaded. At 32 threads that is a third of the wall clock not scaling at all.

Whole processes sidestep both. This is exactly the lesson the widefield side learned, where
--jobs beat --threads by about 2x for the same core count.

Each worker compresses its own copy-free read of the same sample to its own output, so the
aggregate rate is (procs x sample bytes) / wall clock. Writes only under --workdir.

    python3.9 /mnt/data/data/temp/pylibs/sahale_mtscomp_parallel.py \\
        /mnt/data/data/Subjects/AL_0039/2025-09-30/6/p0_g0_t0.imec0/p0_g0_t0.imec0.ap.bin \\
        --gb 2 --procs 1 2 4 8 --threads 4
"""

import argparse
import os
import os.path as op
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, op.dirname(op.abspath(__file__)))

RAW = 94.79e12  # TB of raw ephys on Y:, from the census


def read_meta(path):
    n, rate = 0, 0.0
    with open(path, errors="replace") as fh:
        for line in fh:
            if line.startswith("nSavedChans="):
                n = int(line.split("=", 1)[1])
            elif line.startswith(("imSampRate=", "niSampRate=")):
                rate = float(line.split("=", 1)[1])
    return n, rate


def one(job):
    """Compress the shared sample to a private output. Runs in a child process."""
    sample, out, outm, rate, n_ch, threads = job
    import mtscomp

    for p in (out, outm):
        if op.isfile(p):
            os.remove(p)
    t0 = time.time()
    mtscomp.compress(sample, out, outm, sample_rate=rate, n_channels=n_ch,
                     dtype="int16", n_threads=threads, check_after_compress=True)
    return time.time() - t0, op.getsize(out) + op.getsize(outm)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src")
    ap.add_argument("--gb", type=float, default=2.0)
    ap.add_argument("--procs", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--threads", type=int, default=4, help="threads inside each process")
    ap.add_argument("--workdir", default=op.expanduser("~/mtspar"))
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    n_ch, rate = read_meta(args.src.replace(".bin", ".meta"))
    if not n_ch or not rate:
        sys.exit("could not parse the .meta")

    os.makedirs(args.workdir, exist_ok=True)
    sample = op.join(args.workdir, "sample.ap.bin")
    want = int(args.gb * 1e9) // (2 * n_ch) * (2 * n_ch)
    if not op.isfile(sample) or op.getsize(sample) != want:
        with open(args.src, "rb") as fi, open(sample, "wb") as fo:
            left = want
            while left:
                b = fi.read(min(1 << 24, left))
                if not b:
                    break
                fo.write(b)
                left -= len(b)
    got = op.getsize(sample)
    print(f"sample {got/1e9:.2f} GB, {n_ch} ch @ {rate:g} Hz, "
          f"{args.threads} threads per process\n")

    print("  procs   threads    wall s   aggregate MB/s   projected days for 94.8 TB")
    best = 0.0
    for np_ in args.procs:
        jobs = [(sample, op.join(args.workdir, f"o{i}.cbin"),
                 op.join(args.workdir, f"o{i}.ch"), rate, n_ch, args.threads)
                for i in range(np_)]
        t0 = time.time()
        with ProcessPoolExecutor(np_) as ex:
            list(ex.map(one, jobs))
        wall = time.time() - t0
        agg = np_ * got / 1e6 / wall
        best = max(best, agg)
        print(f"  {np_:5d}   {args.threads:7d}   {wall:7.1f}   {agg:14.1f}   "
              f"{RAW/(agg*1e6)/86400:14.1f}")
        sys.stdout.flush()
        for j in jobs:
            for p in (j[1], j[2]):
                if op.isfile(p):
                    os.remove(p)

    print(f"\nbest aggregate {best:.0f} MB/s -> {RAW/(best*1e6)/86400:.1f} days for the corpus")
    print("(the pool read at 423 MB/s, so anything below that is CPU-bound, not disk-bound)")
    if not args.keep:
        shutil.rmtree(args.workdir, ignore_errors=True)
        print(f"removed {args.workdir}")


if __name__ == "__main__":
    main()
