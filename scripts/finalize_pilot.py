"""Finalise the Phase 1 list: every distinct geometry, both flavours, plus known-awkward cases."""

from __future__ import annotations

import csv
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wfcompress.lab.census import CSV_FIELDS, probe

HERE = Path(__file__).resolve().parents[1]
rows = [r for r in csv.DictReader((HERE / "data" / "tar_census.csv").open(encoding="utf-8"))]

# the 600x600 bucket and the geometry-unknown archives were both squeezed out by the size cap
extra = [r for r in rows if r["tag"] == "Y" and 0 < int(r["bytes"]) < 60e9
         and r["kind"] in ("frame-N", "basler-tiff", "other")]
random.seed(11)
with ThreadPoolExecutor(12) as ex:
    probed = list(ex.map(lambda r: probe(r["path"], "Y"), random.sample(extra, 120)))

by_geom = {}
for p in probed:
    if p.rows and not p.error:
        key = (p.kind, (p.rows, p.cols), p.shift)
        if key not in by_geom or p.bytes < by_geom[key].bytes:
            by_geom[key] = p
unknown = sorted((p for p in probed if "meanImage" in p.error), key=lambda p: p.bytes)

print("smallest per (flavour, geometry, shift):")
for k, p in sorted(by_geom.items(), key=lambda kv: kv[1].bytes):
    print(f"   {p.bytes/1e9:7.2f} GB  {k[0]:12s} {k[1][0]}x{k[1][1]:<4d} shift={k[2]}  {p.path}")
print(f"\ngeometry-unknown archives found: {len(unknown)}")
for p in unknown[:5]:
    print(f"   {p.bytes/1e9:7.2f} GB  {p.path}")

prev = list(csv.DictReader((HERE / "data" / "pilot_census.csv").open(encoding="utf-8")))
chosen: dict[str, object] = {r["path"]: r for r in prev}

# add any geometry we do not already cover, smallest first, and one unknown-geometry case
covered = {(r["kind"], (int(r["rows"]), int(r["cols"])), int(r["shift"])) for r in prev}
for k, p in sorted(by_geom.items(), key=lambda kv: kv[1].bytes):
    if k not in covered and len(chosen) < 9:
        chosen[p.path] = vars(p)
        covered.add(k)
if unknown:
    chosen[unknown[0].path] = vars(unknown[0])

out = HERE / "data" / "pilot_census.csv"
with out.open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
    w.writeheader()
    for v in chosen.values():
        w.writerow(v)

total = sum(int(v["bytes"]) for v in chosen.values())
print(f"\nfinal pilot: {len(chosen)} sessions, {total/1e9:.1f} GB")
for v in chosen.values():
    print(f"   {int(v['bytes'])/1e9:7.2f} GB  {v['kind']:12s} "
          f"{v['rows']}x{v['cols']} shift={v['shift']}  {v['path']}")
