"""End-to-end check of `wfcompress extract` against a real archive on the share.

The unit tests use synthetic tars. This does the same thing to a session the bulk run has already
compressed, and checks the two claims that matter:

1. ``--files`` output is byte-identical to what ``tar -xf`` on the original archive would give -
   compared member by member against the original tar, which is streamed rather than unpacked.
2. ``--bin`` output is exactly ``rows*cols*n_frames*2`` bytes, and frame k of the binary is the
   pixel content of acquisition frame k - checked against the TIFFs from step 1, and separately
   against the frame numbers in the member names, so an ordering bug cannot pass.

Read-only on the server. Writes only under OUT.
"""

from __future__ import annotations

import json
import re
import sys
import tarfile
import time
from pathlib import Path

import numpy as np
import tifffile

from wfcompress import extract, read_meta

OUT = Path(r"D:\temp\wfExtractTest")
LOG = Path(__file__).resolve().parents[1] / "data" / "bulk.jsonl"


def pick_session(max_bytes: int = 2_000_000_000, want_tiff: bool = True) -> dict:
    rows = [json.loads(ln) for ln in LOG.read_text().splitlines() if ln.strip()]
    ok = [r for r in rows if r.get("ok") and r.get("is_tiff") is want_tiff]
    ok = [r for r in ok if r["source_bytes"] <= max_bytes]
    if not ok:
        raise SystemExit("no finished session small enough in the bulk log")
    return min(ok, key=lambda r: r["source_bytes"])


def check(rec: dict) -> int:
    wfz, tar = Path(rec["wfz"]), Path(rec["tar"])
    meta = read_meta(wfz)
    rows, cols = meta["shape"]
    n = meta["n_frames"]
    print(f"\n{'=' * 78}\nsession   {rec['session']}")
    print(f"  wfz     {wfz}  ({rec['output_bytes']/1e9:.2f} GB)")
    print(f"  frames  {n:,} of {rows}x{cols} {meta['dtype']}, shift={meta['shift']}, "
          f"tiff={meta['is_tiff']}")
    OUT.mkdir(parents=True, exist_ok=True)
    tag = "tiff" if meta["is_tiff"] else "raw"

    # ---- 1. files ---------------------------------------------------------------------------
    files_dir = OUT / f"files_{tag}"
    t0 = time.perf_counter()
    r = extract(wfz, files_dir, fmt="files", overwrite=True)
    dt = time.perf_counter() - t0
    print(f"\nextract --files : {r['n_frames']:,} files, {r['bytes_written']/1e9:.2f} GB in "
          f"{dt:.1f} s ({r['bytes_written']/1e6/dt:.0f} MB/s)")
    print(f"  whole-archive pixel SHA-256 re-checked : {r['pixels_verified']}")

    print("  comparing every member against the original tar on the share ...")
    n_checked = 0
    with tarfile.open(tar, "r|") as tf:          # streaming: no seeking on the share
        for m in tf:
            if not m.isfile():
                continue
            ours = files_dir / m.name
            if not ours.exists():
                print(f"  MISSING {m.name}")
                return 1
            if tf.extractfile(m).read() != ours.read_bytes():
                print(f"  DIFFERS {m.name}")
                return 1
            n_checked += 1
    print(f"  {n_checked:,} members byte-identical to the original tar")
    if n_checked != n:
        print(f"  COUNT MISMATCH: tar has {n_checked:,} members, wfz says {n:,}")
        return 1

    # ---- 2. bin -----------------------------------------------------------------------------
    binp = OUT / f"wf_{tag}.bin"
    t0 = time.perf_counter()
    rb = extract(wfz, binp, fmt="bin", overwrite=True)
    dt = time.perf_counter() - t0
    expect = rows * cols * n * 2
    print(f"\nextract --bin   : {binp.stat().st_size:,} bytes in {dt:.1f} s "
          f"({binp.stat().st_size/1e6/dt:.0f} MB/s)")
    print(f"  expected rows*cols*n_frames*2 = {expect:,}   "
          f"{'MATCH' if binp.stat().st_size == expect else 'MISMATCH'}")
    if binp.stat().st_size != expect:
        return 1
    print(f"  source pixels {rb['source_dtype']} -> written {rb['dtype']}  "
          f"(byteswapped={rb['byteswapped']})")

    # order is the thing most likely to be silently wrong, so check it two independent ways
    names = sorted(p for p in files_dir.rglob("*") if p.is_file())
    numbered = []
    for p in names:
        m = re.search(r"(\d+)(?:\.[A-Za-z0-9]+)?$", p.name)
        if m:
            numbered.append((int(m.group(1)), p))
    numbered.sort()
    print(f"  example member names: {[p.name for _, p in numbered[:3]]} ... "
          f"{numbered[-1][1].name}")
    print(f"  frame numbers {numbered[0][0]}..{numbered[-1][0]} "
          f"({len(numbered):,} of {len(names):,} files)")

    mm = np.memmap(binp, dtype=rb["dtype"], mode="r").reshape(n, rows, cols)
    picks = np.unique(np.linspace(0, n - 1, 25).astype(int))
    for k in picks:
        _num, path = numbered[int(k)]
        img = tifffile.imread(path) if meta["is_tiff"] else np.fromfile(
            path, dtype=meta["dtype"]).reshape(rows, cols)
        if not np.array_equal(mm[int(k)], img):
            print(f"  ORDER/CONTENT MISMATCH at acquisition frame {k} ({path.name})")
            return 1
    print(f"  {len(picks)} sampled frames match the correspondingly-numbered file exactly")

    # Whether reordering *changes* anything depends on the naming: zero-padded names sort the same
    # way numerically, so for those archives the two orders coincide. Report which case this is
    # rather than asserting one of them.
    k = min(200, n)
    sto, acq = OUT / f"sto_{tag}.bin", OUT / f"acq_{tag}.bin"
    extract(wfz, sto, fmt="bin", order="storage", overwrite=True, first=0, last=k)
    extract(wfz, acq, fmt="bin", order="acquisition", overwrite=True, first=0, last=k)
    same = sto.read_bytes() == acq.read_bytes()
    padded = len({len(p.name) for _, p in numbered}) == 1
    print(f"  first {k} frames: storage order == acquisition order: {same}  "
          f"(member names {'are' if padded else 'are NOT'} fixed-width, so this is expected)")
    if same != padded:
        print("  UNEXPECTED: ordering behaviour does not match the naming convention")
        return 1

    print(f"  sidecar: {json.loads(Path(rb['sidecar']).read_text())['numpy']}")
    return 0


def main() -> int:
    rc = 0
    for want_tiff in (True, False):
        try:
            rec = pick_session(want_tiff=want_tiff)
        except SystemExit as e:
            print(f"\n(skipping is_tiff={want_tiff}: {e})")
            continue
        rc |= check(rec)
    print(f"\n{'ALL CHECKS PASSED' if rc == 0 else '*** FAILURES ***'}. "
          f"Output left in {OUT} - delete it when you are done.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
