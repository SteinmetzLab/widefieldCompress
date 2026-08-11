"""Snapshot what the offsite backup holds, so two snapshots can be compared.

Answers the question a single look cannot: is the backlog shrinking, growing, or static, and is
the sync back-filling older files or only ever picking up new ones?

B2 records an upload timestamp per object, so the *historical* rate can also be read straight out
of one snapshot without waiting a day - that is what the by-day table at the end shows.

Read-only. Writes a timestamped CSV under data/b2_snapshots/ and diffs against the previous one.

    python scripts/snapshot_b2.py --bucket sahalebackup
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from check_b2_presence import server_to_key, top_level_map

HERE = Path(__file__).resolve().parents[1]
SNAPDIR = HERE / "data" / "b2_snapshots"
FIELDS = ["session", "wfz", "b2_key", "local_bytes", "in_b2", "b2_bytes",
          "b2_uploaded_utc", "error"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--log", default=str(HERE / "data" / "bulk.jsonl"))
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--compare", help="an earlier snapshot CSV; default is the most recent")
    args = ap.parse_args()

    from b2sdk.v2 import B2Api, SqliteAccountInfo

    api = B2Api(SqliteAccountInfo())
    bucket = api.get_bucket_by_name(args.bucket)
    tops = top_level_map(args.bucket)

    rows = [json.loads(ln) for ln in Path(args.log).read_text().splitlines() if ln.strip()]
    latest: dict[str, dict] = {}
    for r in rows:
        if r.get("ok"):
            latest[r["wfz"]] = r
    todo = list(latest.values())
    print(f"{len(todo)} .wfz on the server; querying b2://{args.bucket}/ ...", flush=True)

    def look(r: dict) -> dict:
        key = server_to_key(r["wfz"], "", "", tops)
        out = {"session": r.get("session", ""), "wfz": r["wfz"], "b2_key": key,
               "local_bytes": r.get("output_bytes", 0), "in_b2": "False",
               "b2_bytes": "", "b2_uploaded_utc": "", "error": ""}
        try:
            fi = bucket.get_file_info_by_name(key)
        except Exception as e:  # noqa: BLE001 - "not found" arrives as an exception
            out["error"] = type(e).__name__
            return out
        out["in_b2"] = "True"
        out["b2_bytes"] = fi.size
        ts = getattr(fi, "upload_timestamp", None)
        if ts:
            out["b2_uploaded_utc"] = datetime.fromtimestamp(
                ts / 1000, timezone.utc).isoformat(timespec="seconds")
        return out

    with ThreadPoolExecutor(args.workers) as ex:
        snap = list(ex.map(look, todo))

    SNAPDIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    out_path = SNAPDIR / f"b2_{stamp}.csv"
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(snap)

    have = [r for r in snap if r["in_b2"] == "True"]
    miss = [r for r in snap if r["in_b2"] != "True"]
    hb = sum(int(r["local_bytes"]) for r in have)
    mb = sum(int(r["local_bytes"]) for r in miss)
    print(f"\nin B2     : {len(have):4d}  {hb/1e12:6.2f} TB")
    print(f"not in B2 : {len(miss):4d}  {mb/1e12:6.2f} TB")
    print(f"            {100*hb/max(hb+mb,1):.0f}% of compressed bytes are offsite")

    # --- what the sync actually managed, per day, from B2's own timestamps ------------------
    per_day: dict[str, list[int]] = defaultdict(list)
    for r in have:
        if r["b2_uploaded_utc"]:
            per_day[r["b2_uploaded_utc"][:10]].append(int(r["b2_bytes"] or 0))
    if per_day:
        print("\n.wfz uploaded to B2, by day (B2's own upload timestamps):")
        print("  date          files      GB")
        for day in sorted(per_day):
            n = len(per_day[day])
            print(f"  {day}   {n:5d}  {sum(per_day[day])/1e9:8.1f}")
        gbs = [sum(v) / 1e9 for v in per_day.values()]
        print(f"  {'':12} median {sorted(gbs)[len(gbs)//2]:.0f} GB/day over "
              f"{len(per_day)} days")

    # --- diff against the previous snapshot -------------------------------------------------
    prev = Path(args.compare) if args.compare else None
    if prev is None:
        others = sorted(p for p in SNAPDIR.glob("b2_*.csv") if p != out_path)
        prev = others[-1] if others else None
    if prev is None:
        print("\nno earlier snapshot to compare against; run again later")
        print(f"wrote {out_path}")
        return 0

    old = {r["wfz"]: r for r in csv.DictReader(prev.open(encoding="utf-8"))}
    new_in = [r for r in snap if r["in_b2"] == "True"
              and old.get(r["wfz"], {}).get("in_b2") == "False"]
    still_out = [r for r in miss if old.get(r["wfz"], {}).get("in_b2") == "False"]
    brand_new = [r for r in snap if r["wfz"] not in old]
    gone = [k for k, r in old.items() if r["in_b2"] == "True"
            and any(s["wfz"] == k and s["in_b2"] != "True" for s in snap)]

    print(f"\nversus {prev.name}:")
    print(f"  newly uploaded      : {len(new_in):4d}  "
          f"{sum(int(r['local_bytes']) for r in new_in)/1e9:8.1f} GB")
    print(f"  still not uploaded  : {len(still_out):4d}  "
          f"{sum(int(r['local_bytes']) for r in still_out)/1e9:8.1f} GB")
    print(f"  new .wfz since then : {len(brand_new):4d}")
    if gone:
        print(f"  *** DISAPPEARED FROM B2: {len(gone)} - investigate ***")
    if new_in:
        oldest = min(r["b2_uploaded_utc"] for r in new_in if r["b2_uploaded_utc"])
        print(f"\n  the sync IS back-filling: files written well before the newest upload "
              f"have appeared (earliest new upload stamp {oldest})")
    elif still_out:
        print("\n  nothing was back-filled between the two snapshots")

    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
