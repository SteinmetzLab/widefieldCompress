"""What rate does mtscomp reach at a given thread count, alongside the widefield campaign?

The question this answers is whether the two campaigns have to be run in sequence. mtscomp
defaults n_threads to cpu_count(), which on a machine already running eight compression workers
means both jobs fight. Capping it to the idle headroom should let ephys proceed for close to free.

Measures the same sample at several thread counts, back to back, so the campaign's own variation
biases them all equally. Compression only - mtscomp's verify pass is left on because it would be
left on in production.
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

LOCAL = Path(r"D:\temp\mtsThreads")


def read_meta(p: Path) -> tuple[int, float]:
    n, rate = 0, 0.0
    for line in p.read_text(errors="replace").splitlines():
        if line.startswith("nSavedChans="):
            n = int(line.split("=", 1)[1])
        elif line.startswith(("imSampRate=", "niSampRate=")):
            rate = float(line.split("=", 1)[1])
    return n, rate


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", required=True)
    ap.add_argument("--gb", type=float, default=2.0)
    ap.add_argument("--threads", type=int, nargs="+", default=[2, 4, 8, 16])
    args = ap.parse_args()

    src = Path(args.src)
    n_ch, rate = read_meta(src.with_name(src.name.replace(".bin", ".meta")))
    if not n_ch or not rate:
        raise SystemExit("could not parse the .meta")

    LOCAL.mkdir(parents=True, exist_ok=True)
    local = LOCAL / "sample.ap.bin"
    want = int(args.gb * 1e9) // (2 * n_ch) * (2 * n_ch)
    if not local.is_file() or local.stat().st_size != want:
        with open(src, "rb") as fi, open(local, "wb", buffering=1 << 24) as fo:
            left = want
            while left:
                b = fi.read(min(1 << 24, left))
                if not b:
                    break
                fo.write(b)
                left -= len(b)
    got = local.stat().st_size
    print(f"{got/1e9:.2f} GB sample, {n_ch} ch @ {rate:,.0f} Hz")
    print("(measured with the widefield campaign running - that is the point)\n")

    from mtscomp import compress as mts

    print("  threads      s      MB/s    ratio")
    for th in args.threads:
        out, meta = LOCAL / "s.cbin", LOCAL / "s.ch"
        for p in (out, meta):
            p.unlink(missing_ok=True)
        t0 = time.perf_counter()
        mts(str(local), str(out), str(meta), sample_rate=rate, n_channels=n_ch,
            dtype="int16", n_threads=th, check_after_compress=True)
        dt = time.perf_counter() - t0
        csize = out.stat().st_size + meta.stat().st_size
        print(f"  {th:7d}  {dt:6.1f}  {got/1e6/dt:8.1f}  x{got/csize:.2f}", flush=True)

    shutil.rmtree(LOCAL, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
