"""Which original tars would be safe to delete, and which would not. Deletes nothing.

    python scripts/audit_deletable.py                 # metadata only, seconds
    python scripts/audit_deletable.py --strict        # + re-verify each .wfz today
    python scripts/audit_deletable.py --strict --rehash-tar   # + re-hash the tar itself

The point of the tiers is that the cheap one is cheap enough to run before every deletion batch,
and the expensive one is the thing you run once before deleting anything at all.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from wfcompress.lab.deletion import Audit, inspect

HERE = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=str(HERE / "data" / "bulk.jsonl"))
    ap.add_argument("--out", default=str(HERE / "data" / "deletable_audit.csv"))
    ap.add_argument("--strict", action="store_true",
                    help="re-verify every .wfz reproduces its recorded hash (full read)")
    ap.add_argument("--rehash-tar", action="store_true",
                    help="also re-hash the tar itself (a second full read; implies --strict)")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    strict = args.strict or args.rehash_tar

    rows = [json.loads(ln) for ln in Path(args.log).read_text().splitlines() if ln.strip()]
    ok = [r for r in rows if r.get("ok")]
    # a session can appear more than once across restarts; the last success wins
    latest: dict[str, dict] = {}
    for r in ok:
        latest[r["tar"]] = r
    todo = list(latest.values())
    if args.limit:
        todo = sorted(todo, key=lambda r: r.get("source_bytes", 0))[: args.limit]

    print(f"{len(todo)} archives recorded as compressed "
          f"({sum(r.get('source_bytes', 0) for r in todo)/1e12:.2f} TB of tar)")
    print(f"mode: {'re-verify each .wfz' if strict else 'metadata only'}"
          f"{' and re-hash each tar' if args.rehash_tar else ''}\n", flush=True)

    audit, t0 = Audit(), time.perf_counter()
    for i, rec in enumerate(todo, 1):
        c = inspect(rec, strict=strict, rehash_tar=args.rehash_tar, threads=args.threads)
        audit.candidates.append(c)
        if strict or c.verdict != "SAFE":
            print(f"  [{i}/{len(todo)}] {c.verdict:<6} {c.session:<24} {c.reason}", flush=True)

    audit.write_csv(args.out)
    safe, refused = audit.safe, audit.refused
    tb = sum(c.tar_bytes_now for c in safe) / 1e12
    print(f"\n{'=' * 76}")
    print(f"SAFE to delete : {len(safe):5d} archives, {tb:.2f} TB")
    print(f"REFUSED        : {len(refused):5d} archives, "
          f"{sum(c.tar_bytes_now for c in refused)/1e12:.2f} TB")
    for reason, n in Counter(c.reason for c in refused).most_common():
        print(f"    {n:4d}  {reason[:88]}")
    print(f"\nelapsed {time.perf_counter()-t0:.0f} s; wrote {args.out}")
    print("\nNothing was deleted. This script has no delete path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
