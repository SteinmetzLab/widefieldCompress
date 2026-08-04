"""Add an age column to the .phy inventory and break it down, so it can be filtered before deleting.

Some of these are days old. A `.phy` folder holds the manual curation state for a sorting - cluster
labels, merges, splits - so a recent one may be someone's work in progress rather than litter.
"""

from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SRC = HERE / "data" / "phy_folders_Y.csv"

rows = list(csv.DictReader(SRC.open(encoding="utf-8")))
now = datetime.now(timezone.utc)

for r in rows:
    r["size_bytes"] = int(r["size_bytes"])
    r["n_files"] = int(r["n_files"])
    newest = r["newest_mtime_utc"]
    r["days_since_modified"] = (
        (now - datetime.fromisoformat(newest)).days if newest else ""
    )

fields = list(rows[0].keys())
with SRC.open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=fields)
    w.writeheader()
    w.writerows(sorted(rows, key=lambda r: -r["size_bytes"]))

total = sum(r["size_bytes"] for r in rows)
print(f"{len(rows)} .phy folders, {total/1e9:.1f} GB, {sum(r['n_files'] for r in rows):,} files\n")

print("by age of the most recently modified file inside:")
buckets = [(7, "under a week"), (30, "1 week - 1 month"), (90, "1 - 3 months"),
           (365, "3 - 12 months"), (10**6, "over a year")]
prev = -1
for limit, label in buckets:
    sel = [r for r in rows if isinstance(r["days_since_modified"], int)
           and prev < r["days_since_modified"] <= limit]
    prev = limit
    if sel:
        print(f"  {label:<18s} {len(sel):3d} folders  {sum(r['size_bytes'] for r in sel)/1e9:6.2f} GB")

print("\nby subject (top 12 by size):")
by_sub: dict[str, list] = {}
for r in rows:
    by_sub.setdefault(r["subject"], []).append(r)
for sub, rs in sorted(by_sub.items(), key=lambda kv: -sum(r["size_bytes"] for r in kv[1]))[:12]:
    print(f"  {sub:<28s} {len(rs):3d} folders  {sum(r['size_bytes'] for r in rs)/1e9:6.2f} GB")
print(f"  ({len(by_sub)} subjects in total)")

recent = [r for r in rows if isinstance(r["days_since_modified"], int)
          and r["days_since_modified"] <= 30]
if recent:
    print(f"\nmodified within the last 30 days - worth asking about before deleting "
          f"({len(recent)} folders, {sum(r['size_bytes'] for r in recent)/1e9:.2f} GB):")
    for r in sorted(recent, key=lambda r: r["days_since_modified"]):
        print(f"  {r['days_since_modified']:3d} d ago  {r['size_bytes']/1e9:6.2f} GB  "
              f"{r['subject']:<12s} {r['path']}")

print(f"\nupdated {SRC} with a days_since_modified column")
