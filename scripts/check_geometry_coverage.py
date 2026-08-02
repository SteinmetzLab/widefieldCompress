"""How many Y: archives are blocked because their frame geometry cannot be recovered?

TIFF members carry their own geometry. Headerless ones do not, and the only authoritative source
in a session folder is blue/ or violet/ meanImage.npy. Where that is missing the archive cannot be
compressed without someone supplying the shape by hand.
"""

from __future__ import annotations

import csv
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wfcompress.lab.session import has_svd, session_frame_shape

HERE = Path(__file__).resolve().parents[1]
rows = [
    r for r in csv.DictReader((HERE / "data" / "tar_census.csv").open(encoding="utf-8"))
    if r["tag"] == "Y" and int(r["bytes"]) > 0
]


def check(r):
    p = Path(r["path"])
    headerless = r["kind"] in ("frame-N", "other")
    shape = session_frame_shape(p) if headerless else None
    return {
        "path": r["path"],
        "bytes": int(r["bytes"]),
        "kind": r["kind"],
        "headerless": headerless,
        "shape": f"{shape[0]}x{shape[1]}" if shape else "",
        "blocked": headerless and shape is None,
        "has_svd": has_svd(p),
    }


with ThreadPoolExecutor(24) as ex:
    out = list(ex.map(check, rows))

blocked = [r for r in out if r["blocked"]]
headerless = [r for r in out if r["headerless"]]

print(f"Y: archives examined         : {len(out):5d}   {sum(r['bytes'] for r in out)/1e12:6.2f} TB")
print(f"  TIFF (geometry self-evident): {len(out)-len(headerless):5d}   "
      f"{sum(r['bytes'] for r in out if not r['headerless'])/1e12:6.2f} TB")
print(f"  headerless                  : {len(headerless):5d}   "
      f"{sum(r['bytes'] for r in headerless)/1e12:6.2f} TB")
print(f"    of which geometry known   : {len(headerless)-len(blocked):5d}")
print(f"    BLOCKED, no meanImage.npy : {len(blocked):5d}   "
      f"{sum(r['bytes'] for r in blocked)/1e12:6.2f} TB")

print(f"\nshapes found for headerless archives: "
      f"{Counter(r['shape'] for r in headerless if r['shape']).most_common()}")

if blocked:
    print("\nblocked archives:")
    for r in sorted(blocked, key=lambda r: -r["bytes"]):
        print(f"  {r['bytes']/1e9:8.1f} GB  svd={'y' if r['has_svd'] else 'n'}  {r['path']}")

with (HERE / "data" / "geometry_coverage.csv").open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=list(out[0].keys()))
    w.writeheader()
    w.writerows(out)
print("\nwrote data/geometry_coverage.csv")
