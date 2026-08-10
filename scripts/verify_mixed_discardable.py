"""Prove, for every mixed archive, that the non-frame members exist outside the tar.

Read-only, and resumable: each archive's result is appended to the manifest as it completes, so
an interrupted run picks up where it left off. Hashing both copies of ~1.2 TB of ephys takes a
couple of hours over the share; ``--quick`` checks name and size only, which is enough to plan
with but not enough to delete on.

    python scripts/verify_mixed_discardable.py            # full sha256, resumable
    python scripts/verify_mixed_discardable.py --quick    # size only, minutes
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from wfcompress.lab import mixed

HERE = Path(__file__).resolve().parents[1]
DETAIL = HERE / "data" / "mixed_archives_detail.csv"
MANIFEST = HERE / "data" / "mixed_discard_manifest.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="size match only, no hashing")
    ap.add_argument("--manifest", default=str(MANIFEST))
    ap.add_argument("--only", help="substring filter on the archive path")
    ap.add_argument("--redo", action="store_true", help="ignore existing results")
    args = ap.parse_args()

    tars = [r["path"] for r in csv.DictReader(DETAIL.open(encoding="utf-8"))
            if r["uniform"] == "False"]
    if args.only:
        tars = [t for t in tars if args.only in t]

    manifest_path = Path(args.manifest)
    done: dict[str, dict] = {}
    if manifest_path.exists() and not args.redo:
        done = mixed.read_manifest(manifest_path)
        # a quick result must not satisfy a full run
        if not args.quick:
            done = {k: v for k, v in done.items()
                    if all(d.get("method") == "sha256" for d in v.get("discardable", []))
                    and v.get("all_verified")}
        print(f"{len(done)} archives already verified in {manifest_path.name}")

    todo = [t for t in tars if t not in done]
    print(f"{len(todo)} of {len(tars)} archives to check "
          f"({'size only' if args.quick else 'full sha256 of both copies'})\n", flush=True)

    records: list[mixed.MixedArchive] = []
    for k, tar in enumerate(todo, 1):
        t0 = time.perf_counter()
        p = Path(tar)
        print(f"[{k}/{len(todo)}] {'/'.join(p.parts[-4:-1])}", flush=True)

        def report(i, n, e, _t0=t0):
            print(f"      member {i+1}/{n}  {e.name}  {e.size/1e9:.1f} GB", flush=True)

        rec = mixed.inspect(tar, quick=args.quick, progress=report)
        records.append(rec)
        dt = time.perf_counter() - t0
        if rec.error:
            print(f"      ERROR: {rec.error}")
        else:
            print(f"      {rec.n_frames:,} frames of {rec.frame_bytes:,} B "
                  f"({rec.frames_total_bytes/1e9:.1f} GB) | "
                  f"{len(rec.discardable)} to discard ({rec.discard_bytes/1e9:.1f} GB) | "
                  f"{'ALL VERIFIED' if rec.all_verified else '*** NOT ALL VERIFIED ***'} "
                  f"| {dt/60:.1f} min")
            for d in rec.discardable:
                if not d.verified:
                    print(f"      UNVERIFIED  {d.member}  ({d.member_bytes/1e9:.2f} GB)")
                    print(f"                  {d.note}")

        # append as we go, so the run is resumable
        blob = dict(done)
        for r in records:
            blob[r.tar] = json.loads(json.dumps({
                "tar_bytes": r.tar_bytes, "frame_bytes": r.frame_bytes, "n_frames": r.n_frames,
                "all_verified": r.all_verified, "discard_bytes": r.discard_bytes,
                "discardable": [d.__dict__ for d in r.discardable], "error": r.error,
            }))
        manifest_path.write_text(json.dumps(blob, indent=1, sort_keys=True), encoding="utf-8")

    allrecs = records
    ok = [r for r in allrecs if r.all_verified]
    print(f"\n{'=' * 74}")
    print(f"fully verified : {len(ok)} of {len(allrecs)} archives checked this run")
    print(f"frames to keep : {sum(r.frames_total_bytes for r in ok)/1e12:.2f} TB")
    print(f"to be dropped  : {sum(r.discard_bytes for r in ok)/1e12:.2f} TB "
          f"(proven to exist elsewhere)")
    problems = [r for r in allrecs if not r.all_verified]
    if problems:
        print(f"\n{len(problems)} archive(s) NOT safe to trim:")
        for r in problems:
            print(f"  {r.tar}\n    {r.error or 'some members unverified'}")
    print(f"\nmanifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
