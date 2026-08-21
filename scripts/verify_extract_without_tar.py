"""Can someone still get usable data out of a session whose original tar has been deleted?

`wfcompress check` proves a .wfz reproduces the tar's *bytes*. That is not the same claim as "a
scientist can retrieve the frames", because retrieval goes through `wfcompress extract`, which is
separate code and is what anyone will actually run. Every deletion so far has been gated on
byte-recoverability alone. This closes that gap.

The original tar is gone for these sessions, so there is nothing to diff against. Five checks that
need no tar:

1. ``extract`` (no --bin) writes exactly ``n_frames`` frame files.
2. The **pixel window** of each extracted member - ``meta['px_start']`` to ``px_start+px_len``, in
   the source endianness, in storage order - hashes to the receipt's ``pixels_sha256``. That is an
   independent value written at compression time, so a match is a real comparison rather than a
   tautology. Note it covers pixels only, *not* the TIFF shell (4,626 bytes per frame on the file
   this was developed against), so hashing whole files does not reproduce it.
3. ``extract --bin`` is exactly ``rows*cols*n_frames*2`` bytes.
4. Frame k of the storage-order binary equals frame file k. Storage order is used so the files
   line up by name with no private index, while a decode or slot-mapping error still shows.
5. The acquisition-order binary is a permutation of the storage-order one - same multiset of
   frames, nothing lost or duplicated. This is the check that would catch an ordering bug, the
   failure mode with scientific consequences: silently permuted or dropped frames would corrupt
   every downstream analysis while looking perfectly healthy.

**Affordability.** Extraction materialises the frames, so this costs roughly the size of the
original tar in local disk. That is fine for small archives and impossible for the large ones
(430 GB against ~104 GB free). For those, ``delete_tar.py check`` C6/C10 remain the evidence, and
``extract --frames FIRST LAST`` would give a partial version of checks 1, 4 and 5.

Read-only on the share. Writes only under --out, and removes it unless --keep.

    python scripts/verify_extract_without_tar.py --session test/2026-02-17/1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", required=True)
    ap.add_argument("--server", default=r"\\sahale.biostr.washington.edu\data")
    ap.add_argument("--log", default=str(HERE / "data" / "bulk.jsonl"))
    ap.add_argument("--out", default=r"D:\temp\extract_check")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--max-frames-compared", type=int, default=40,
                    help="frames to compare pixel-for-pixel in check 4")
    args = ap.parse_args()

    import numpy as np
    import tifffile

    from wfcompress import extract, read_meta

    sess = args.session.strip("/").replace("\\", "/")
    rec = None
    for line in Path(args.log).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r.get("ok") and r["session"] == sess:
                rec = r
    if rec is None:
        print(f"no successful run-log record for {sess}")
        return 2

    p = rec["wfz"].replace("\\", "/")
    wfz = Path(args.server) / p[3:] if len(p) > 2 and p[1] == ":" else Path(rec["wfz"])
    receipt = json.loads(wfz.with_name(wfz.name + ".receipt.json").read_text(encoding="utf-8"))
    tar_present = wfz.with_name(Path(rec["tar"]).name).exists()

    meta = read_meta(str(wfz))
    rows, cols = meta["shape"]
    n = meta["n_frames"]
    print(f"session   {sess}")
    print(f".wfz      {wfz}")
    print(f"original tar still on the server: {tar_present}"
          + ("" if tar_present else "   <-- this is the case worth testing"))
    print(f"shape {rows}x{cols}  n_frames {n}  is_tiff {meta['is_tiff']}  "
          f"dtype {meta.get('dtype')}\n")

    work = Path(args.out) / sess.replace("/", "_")
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    files_dir = work / "frames"
    bin_store, bin_acq = work / "storage.bin", work / "acquisition.bin"
    files_dir.mkdir(parents=True)
    results: list[tuple[str, bool, str]] = []

    # ---- 1: extract the frame files -------------------------------------------------------
    print("[1] extract -> frame files ...", flush=True)
    extract(str(wfz), str(files_dir), fmt="files", overwrite=True)
    got = sorted((q for q in files_dir.rglob("*") if q.is_file()), key=lambda q: q.name)
    ok = len(got) == n
    print(f"    {len(got)} files written, expected {n}")
    results.append(("1  extract writes n_frames frame files", ok,
                    "" if ok else f"{len(got)} != {n}"))

    # ---- 2: the pixel payloads must hash to the recorded pixels_sha256 --------------------
    # `pixels_sha256` covers only the pixel window of each member - meta['px_start'] to
    # px_start+px_len, in the *source* endianness - taken in storage order, i.e. the order the
    # member names sort in. It deliberately excludes the TIFF shell (4,626 bytes per frame here),
    # which is why hashing whole files does not reproduce it. Confirmed two ways on
    # test/2026-02-17/1: the raw byte window and the decoded pixels re-encoded as >u2 both give
    # the recorded value. This is an independent number written at compression time, so a match
    # is a real comparison and not a tautology.
    px_start = meta.get("px_start", 0)
    px_len = meta.get("px_len", rows * cols * 2)
    print(f"[2] hashing the extracted pixel payloads ([{px_start}:{px_start + px_len}] of each "
          f"member) against the receipt's pixels_sha256 ...", flush=True)
    h = hashlib.sha256()
    for q in got:
        h.update(q.read_bytes()[px_start:px_start + px_len])
    same = h.hexdigest() == receipt.get("pixels_sha256")
    print(f"    extracted {h.hexdigest()}")
    print(f"    receipt   {receipt.get('pixels_sha256')}")
    results.append(("2  extracted pixel payloads hash to the recorded pixels_sha256", same,
                    "" if same else "MISMATCH"))

    # ---- 3: --bin is exactly the right size ----------------------------------------------
    print("[3] extract --bin, storage order ...", flush=True)
    extract(str(wfz), str(bin_store), fmt="bin", order="storage", overwrite=True)
    expect_bytes = rows * cols * n * 2
    actual = bin_store.stat().st_size
    ok = actual == expect_bytes
    print(f"    {actual} bytes, expected {rows}*{cols}*{n}*2 = {expect_bytes}")
    results.append(("3  --bin is rows*cols*n_frames*2 bytes", ok,
                    "" if ok else f"{actual} != {expect_bytes}"))

    # ---- 4: frame k of the storage-order binary is the k-th frame file --------------------
    # Storage order is used deliberately: it is the order the files sort in, so no private index
    # is needed to line them up, and a decode or slot-mapping error still shows.
    print("[4] comparing frames from the binary against the extracted images ...", flush=True)
    flat = np.memmap(bin_store, dtype="<u2", mode="r", shape=(n, rows, cols))
    step = max(1, n // max(args.max_frames_compared, 1))
    idx = list(range(0, n, step))[:args.max_frames_compared]
    bad = []
    for k in idx:
        raw = got[k].read_bytes()
        if meta["is_tiff"]:
            img = np.asarray(tifffile.imread(str(got[k])))
        else:
            img = np.frombuffer(raw[-rows * cols * 2:], dtype="<u2").reshape(rows, cols)
        # extract --bin normalises to little-endian; the source may be big-endian TIFF
        img = np.asarray(img).astype("<u2", copy=False)
        if not np.array_equal(np.asarray(flat[k]), img):
            bad.append(k)
    ok = not bad
    print(f"    compared {len(idx)} frames spread across the recording; mismatches: {len(bad)}")
    results.append((f"4  binary frame k == frame file k ({len(idx)} sampled)", ok,
                    "" if ok else f"frames {bad[:8]} differ"))

    # ---- 5: acquisition order is a permutation, losing and duplicating nothing ------------
    print("[5] extract --bin, acquisition order, and check it is a permutation ...", flush=True)
    extract(str(wfz), str(bin_acq), fmt="bin", order="acquisition", overwrite=True)
    acq = np.memmap(bin_acq, dtype="<u2", mode="r", shape=(n, rows, cols))
    hs_store = sorted(hashlib.sha256(np.asarray(flat[k]).tobytes()).hexdigest() for k in range(n))
    hs_acq = sorted(hashlib.sha256(np.asarray(acq[k]).tobytes()).hexdigest() for k in range(n))
    ok = hs_store == hs_acq
    nperm = sum(1 for k in range(n)
                if not np.array_equal(np.asarray(flat[k]), np.asarray(acq[k])))
    print(f"    same multiset of frames: {ok}; {nperm} of {n} frames sit at a different index")
    results.append(("5  acquisition order is a permutation of storage order", ok,
                    "" if ok else "frames lost or duplicated"))

    del flat, acq
    print("\n" + "=" * 78)
    for name, good, note in results:
        print(f"  {'PASS' if good else 'FAIL'}  {name}" + (f"   ({note})" if note else ""))
    print("=" * 78)
    every = all(g for _, g, _ in results)
    print("  The retrieval path works on an archive whose tar is gone."
          if every else "  RETRIEVAL PROBLEM - do not delete more tars.")

    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if every else 1


if __name__ == "__main__":
    raise SystemExit(main())
