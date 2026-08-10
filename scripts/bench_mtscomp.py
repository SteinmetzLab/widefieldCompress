"""Measure what mtscomp actually achieves on this lab's ephys, rather than trusting the README.

mtscomp's own figures are ~3x on 385-channel 30 kHz Neuropixels. Worth checking against a real
file here before sizing a campaign around it: ratio depends on the noise floor, the probe type,
and whether the recording was filtered or gain-corrected on the way out of SpikeGLX.

Copies a truncated prefix of a real ``.ap.bin`` to local disk (whole samples only), compresses it,
and reports ratio and speed. mtscomp verifies its own output by decompressing and comparing, which
is left on.
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

LOCAL = Path(r"D:\temp\mtsBench")


def read_meta(meta_path: Path) -> tuple[int, float]:
    n, rate = 0, 0.0
    for line in meta_path.read_text(errors="replace").splitlines():
        if line.startswith("nSavedChans="):
            n = int(line.split("=", 1)[1])
        elif line.startswith(("imSampRate=", "niSampRate=")):
            rate = float(line.split("=", 1)[1])
    return n, rate


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", required=True, help="a real .ap.bin on the share")
    ap.add_argument("--gb", type=float, default=4.0, help="how much of it to copy locally")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    src = Path(args.src)
    meta_src = src.with_name(src.name.replace(".bin", ".meta"))
    if not meta_src.is_file():
        raise SystemExit(f"no .meta beside {src}")
    n_ch, rate = read_meta(meta_src)
    if not n_ch or not rate:
        raise SystemExit(f"could not parse nSavedChans / sample rate from {meta_src}")
    print(f"source : {src}")
    print(f"         {src.stat().st_size/1e9:.1f} GB, {n_ch} channels @ {rate:,.0f} Hz")

    LOCAL.mkdir(parents=True, exist_ok=True)
    sample_bytes = 2 * n_ch
    want = int(args.gb * 1e9) // sample_bytes * sample_bytes
    local = LOCAL / "sample.ap.bin"

    if not local.is_file() or local.stat().st_size != want:
        t0 = time.perf_counter()
        with open(src, "rb") as fi, open(local, "wb", buffering=1 << 24) as fo:
            left = want
            while left:
                chunk = fi.read(min(1 << 24, left))
                if not chunk:
                    break
                fo.write(chunk)
                left -= len(chunk)
        got = local.stat().st_size
        dt = time.perf_counter() - t0
        print(f"copied {got/1e9:.2f} GB locally in {dt:.0f} s ({got/1e6/dt:.0f} MB/s)")
    got = local.stat().st_size
    seconds = got / (sample_bytes * rate)
    print(f"         = {seconds:.0f} s of recording\n")

    from mtscomp import compress as mts_compress
    from mtscomp import decompress as mts_decompress

    out = LOCAL / "sample.ap.cbin"
    outmeta = LOCAL / "sample.ap.ch"
    for p in (out, outmeta):
        p.unlink(missing_ok=True)

    t0 = time.perf_counter()
    mts_compress(str(local), str(out), str(outmeta),
                 sample_rate=rate, n_channels=n_ch, dtype="int16",
                 check_after_compress=True)
    dt = time.perf_counter() - t0
    csize = out.stat().st_size + outmeta.stat().st_size
    print(f"\nmtscomp: {got/1e9:.2f} GB -> {csize/1e9:.2f} GB   "
          f"x{got/csize:.2f}   {100*(1-csize/got):.1f}% saved")
    print(f"         {dt:.0f} s = {got/1e6/dt:.0f} MB/s  (includes its own verify pass)")
    print(f"         .ch index is {outmeta.stat().st_size/1e6:.2f} MB")

    back = LOCAL / "roundtrip.bin"
    back.unlink(missing_ok=True)
    t0 = time.perf_counter()
    mts_decompress(str(out), str(outmeta), out=str(back), check_after_decompress=True)
    dtd = time.perf_counter() - t0
    same = back.stat().st_size == got
    print(f"         decompress {dtd:.0f} s = {got/1e6/dtd:.0f} MB/s, size matches: {same}")

    import hashlib

    def sha(p):
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            while c := fh.read(1 << 24):
                h.update(c)
        return h.hexdigest()

    a, b = sha(local), sha(back)
    print(f"         sha256 {'IDENTICAL' if a == b else '*** DIFFERENT ***'}")

    if not args.keep:
        shutil.rmtree(LOCAL, ignore_errors=True)
    return 0 if a == b else 1


if __name__ == "__main__":
    raise SystemExit(main())
