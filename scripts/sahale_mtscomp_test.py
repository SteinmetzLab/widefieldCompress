"""Timed mtscomp run, for use ON sahale where there is no pip.

mtscomp's CLI is installed as a console script, which needs pip. The module itself is a single
pure-Python file, so staging it plus tqdm on the share and importing directly sidesteps that
entirely - no pip, no compiler, no root, nothing written outside the scratch directory.

Copy this next to the staged libraries and run:

    python3.9 /mnt/<pool>/temp/pylibs/sahale_mtscomp_test.py \
        /mnt/<pool>/Subjects/AL_0039/2025-09-30/6/p0_g0_t0.imec0/p0_g0_t0.imec0.ap.bin \
        --gb 4 --threads 8 16 32

Reads a prefix of a real recording straight off the pool, compresses it at each thread count,
and reports the rate. Writes only into --workdir (default ~/mtstest) and cleans up after itself.
mtscomp's own verify pass is left on, as it would be in production.
"""

import argparse
import os
import os.path as op
import shutil
import sys
import time

sys.path.insert(0, op.dirname(op.abspath(__file__)))


def read_meta(path):
    n, rate = 0, 0.0
    with open(path, errors="replace") as fh:
        for line in fh:
            if line.startswith("nSavedChans="):
                n = int(line.split("=", 1)[1])
            elif line.startswith(("imSampRate=", "niSampRate=")):
                rate = float(line.split("=", 1)[1])
    return n, rate


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src", help="a real .ap.bin on the pool")
    ap.add_argument("--gb", type=float, default=4.0)
    ap.add_argument("--threads", type=int, nargs="+", default=[8, 16, 32])
    ap.add_argument("--workdir", default=op.expanduser("~/mtstest"))
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    meta_path = args.src.replace(".bin", ".meta")
    if not op.isfile(meta_path):
        sys.exit("no .meta beside the .bin")
    n_ch, rate = read_meta(meta_path)
    if not n_ch or not rate:
        sys.exit("could not parse nSavedChans / sample rate")

    os.makedirs(args.workdir, exist_ok=True)
    sample = op.join(args.workdir, "sample.ap.bin")
    want = int(args.gb * 1e9) // (2 * n_ch) * (2 * n_ch)

    print("source  %s" % args.src)
    print("        %d channels @ %g Hz, %.1f GB on the pool"
          % (n_ch, rate, op.getsize(args.src) / 1e9))

    if not op.isfile(sample) or op.getsize(sample) != want:
        t0 = time.time()
        with open(args.src, "rb") as fi, open(sample, "wb") as fo:
            left = want
            while left:
                b = fi.read(min(1 << 24, left))
                if not b:
                    break
                fo.write(b)
                left -= len(b)
        dt = time.time() - t0
        got = op.getsize(sample)
        print("        copied %.2f GB locally in %.0f s = %.0f MB/s  <- local pool read rate"
              % (got / 1e9, dt, got / 1e6 / dt))
    got = op.getsize(sample)

    import mtscomp

    print("\n  threads      s      MB/s    ratio")
    for th in args.threads:
        out = op.join(args.workdir, "s.cbin")
        outm = op.join(args.workdir, "s.ch")
        for p in (out, outm):
            if op.isfile(p):
                os.remove(p)
        t0 = time.time()
        mtscomp.compress(sample, out, outm, sample_rate=rate, n_channels=n_ch,
                         dtype="int16", n_threads=th, check_after_compress=True)
        dt = time.time() - t0
        csize = op.getsize(out) + op.getsize(outm)
        print("  %7d  %6.1f  %8.1f  x%.2f" % (th, dt, got / 1e6 / dt, got / csize))
        sys.stdout.flush()

    print("\n94.79 TB of raw ephys; at the best rate above that is:")
    print("  (divide 94.79e12 by the MB/s figure, /86400, for days)")
    if not args.keep:
        shutil.rmtree(args.workdir, ignore_errors=True)
        print("\nremoved %s" % args.workdir)


if __name__ == "__main__":
    main()
