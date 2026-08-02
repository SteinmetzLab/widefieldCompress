"""Which tar'd sessions have NO SVD output? Those are the ones where the tar is the only copy."""

import csv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

rows = [r for r in csv.DictReader(Path("tar_census.csv").open(encoding="utf-8"))]
rows = [r for r in rows if int(r["bytes"]) > 0]


def check(r):
    d = Path(r["path"]).parent
    blue = d / "blue" / "svdSpatialComponents.npy"
    return {
        "path": r["path"],
        "bytes": int(r["bytes"]),
        "kind": r["kind"],
        "has_svd": blue.is_file(),
        "svd_bytes": blue.stat().st_size if blue.is_file() else 0,
    }


with ThreadPoolExecutor(max_workers=24) as ex:
    out = list(ex.map(check, rows))

no = [r for r in out if not r["has_svd"]]
yes = [r for r in out if r["has_svd"]]
print(f"tars with SVD alongside : {len(yes):5d}   {sum(r['bytes'] for r in yes)/1e12:6.1f} TB")
print(f"tars with NO SVD        : {len(no):5d}   {sum(r['bytes'] for r in no)/1e12:6.1f} TB")
print(f"\ntotal SVD footprint (blue spatial only): "
      f"{sum(r['svd_bytes'] for r in yes)/1e12:.2f} TB")

print("\nsessions with no SVD (tar is the only copy) — first 25:")
for r in sorted(no, key=lambda r: -r["bytes"])[:25]:
    print(f"  {r['bytes']/1e9:8.1f} GB  {r['kind']:12s} {r['path']}")

with open("no_svd.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=["path", "bytes", "kind", "has_svd", "svd_bytes"])
    w.writeheader()
    w.writerows(no)
print(f"\nwrote no_svd.csv ({len(no)} rows)")
