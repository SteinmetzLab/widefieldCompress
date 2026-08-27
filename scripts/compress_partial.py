"""Compress session archives in **partial** mode, dropping members proven to exist outside.

Five archives refused normal compression because a SpikeGLX log,
``1/p0.missed_samples.imec0.txt``, was swept into the widefield tar. Partial mode keeps the frames
and drops that member - but only on the evidence produced by ``wfcompress.lab.mixed``, which hashes
the member inside the tar and finds a byte-identical copy outside. ``codec.compress`` refuses
anything not in that manifest.

The result deliberately does **not** reproduce the source tar. It reproduces a tar of the frames
alone, so ``source_tar_sha256`` is null and ``frames_tar_sha256`` carries the guarantee instead.
That means **the deletion gate will refuse these archives**, by design: condition C3 requires
``source_tar_sha256``. Deleting one of these tars is a separate decision that has to account for
the dropped member being preserved elsewhere.

    python scripts/compress_partial.py --manifest data/mixed_manifest_20260827.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--log", default=str(HERE / "data" / "bulk.jsonl"))
    ap.add_argument("--file-log", default=str(HERE / "data" / "fileEditLog.csv"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from wfcompress import codec, sidecar
    from wfcompress.lab import mixed, session

    manifest = mixed.read_manifest(args.manifest)
    tars = sorted(manifest)
    print(f"{len(tars)} archive(s) in {args.manifest}\n")

    done = set()
    logp = Path(args.log)
    if logp.exists():
        for ln in logp.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                r = json.loads(ln)
                if r.get("ok"):
                    done.add(os.path.normcase(r["tar"]))

    ok = skipped = failed = 0
    for tar in tars:
        tarp = Path(tar)
        drop = mixed.verified_members(manifest, tarp)
        sess = session.session_id(tarp)
        print(f"{'=' * 74}\n{sess}")
        if not drop:
            print("  no fully-verified evidence in the manifest - refusing")
            skipped += 1
            continue
        if os.path.normcase(str(tarp)) in done:
            print("  already recorded as compressed - skipping")
            skipped += 1
            continue
        out = tarp.with_name(tarp.stem + ".wfz")
        if out.exists():
            print(f"  output already exists: {out}")
            skipped += 1
            continue
        for m, ev in drop.items():
            print(f"  dropping {m} ({ev['member_bytes']} B), verified by {ev['method']}")
            print(f"    outside: {ev['outside_path']}")
        if args.dry_run:
            print("  --dry-run, not compressing")
            continue

        t0 = time.perf_counter()
        try:
            meta = codec.compress(tarp, out, threads=args.threads, drop_members=drop,
                                  file_log=args.file_log)
            v = codec.verify(out, threads=args.threads)
        except Exception as e:  # noqa: BLE001 - one bad archive must not stop the rest
            print(f"  FAILED: {type(e).__name__}: {e}")
            failed += 1
            continue
        meta["byte_identical_verified"] = bool(v.get("byte_identical"))
        sidecar.write_readme(out, meta, file_log=args.file_log)
        sidecar.write_preview_frame(out, file_log=args.file_log)
        sidecar.write_receipt(out, meta,
                              extra={"frames_tar_sha256": v.get("tar_sha256"),
                                     "byte_identical": v.get("byte_identical")},
                              file_log=args.file_log)
        rec = {
            "tar": str(tarp), "wfz": str(out), "session": sess,
            "ratio": meta["ratio"], "source_bytes": meta["source_bytes"],
            "output_bytes": out.stat().st_size, "shift": meta["shift"],
            "n_frames": meta["n_frames"], "shape": meta["shape"],
            "is_tiff": meta["is_tiff"],
            "partial": True,
            "tar_sha256": None,                      # a partial does not reproduce the source
            "frames_tar_sha256": v.get("tar_sha256"),
            "byte_identical": bool(v.get("byte_identical")),
            "dropped_members": meta.get("dropped_members", []),
            "dropped_bytes": meta.get("dropped_bytes", 0),
            "verified_by": "stream", "elapsed_s": time.perf_counter() - t0,
            "compressed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ok": True,
        }
        with logp.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"  x{meta['ratio']:.2f}  shift={meta['shift']}  "
              f"frames rebuild {'OK' if v.get('byte_identical') else 'MISMATCH'}  "
              f"{(time.perf_counter()-t0)/60:.1f} min")
        ok += 1

    print(f"\n{'=' * 74}\n  compressed {ok}   skipped {skipped}   failed {failed}")
    print("  NOTE: these are partial archives. source_tar_sha256 is null, so the deletion gate\n"
          "  will refuse them - deleting their tars needs a separate, recorded decision.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
