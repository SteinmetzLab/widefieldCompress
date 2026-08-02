import csv
from collections import Counter, defaultdict
from pathlib import Path

rows = list(csv.DictReader(Path("tar_census.csv").open(encoding="utf-8")))
for r in rows:
    r["bytes"] = int(r["bytes"])
    r["member_bytes"] = int(r["member_bytes"]) if r["member_bytes"] else 0
    r["est_frames"] = int(r["est_frames"]) if r["est_frames"] else 0

tot = sum(r["bytes"] for r in rows)
print(f"{len(rows)} tars, {tot/1e12:.1f} TB\n")

print("by format:")
n_k, b_k, f_k = Counter(), defaultdict(int), defaultdict(int)
for r in rows:
    n_k[r["kind"]] += 1
    b_k[r["kind"]] += r["bytes"]
    f_k[r["kind"]] += r["est_frames"]
for k, n in n_k.most_common():
    print(f"  {k:16s} n={n:5d}  {b_k[k]/1e12:7.2f} TB  {f_k[k]/1e6:8.1f} M frames")

print("\nby server:")
for tag in ("Y", "Z"):
    sub = [r for r in rows if r["tag"] == tag]
    print(f"  {tag}: n={len(sub):5d}  {sum(r['bytes'] for r in sub)/1e12:6.1f} TB")

print("\nsize distribution of individual tars:")
sz = sorted(r["bytes"] for r in rows)
import statistics
for q, lbl in [(0, "min"), (25, "p25"), (50, "median"), (75, "p75"), (95, "p95"), (100, "max")]:
    i = min(len(sz) - 1, q * len(sz) // 100)
    print(f"  {lbl:>6s}: {sz[i]/1e9:8.1f} GB")
print(f"  mean  : {statistics.mean(sz)/1e9:8.1f} GB")

wf = [r for r in rows if r["kind"] in ("frame-N", "basler-tiff")]
print(f"\nwidefield tars in scope: n={len(wf)}  {sum(r['bytes'] for r in wf)/1e12:.1f} TB  "
      f"{sum(r['est_frames'] for r in wf)/1e6:.0f} M frames")
print("distinct member sizes among widefield tars:",
      len({r["member_bytes"] for r in wf}))

print("\nNON-widefield / odd entries (exclude from any job):")
for r in rows:
    if r["kind"] not in ("frame-N", "basler-tiff"):
        print(f"  {r['kind']:12s} {r['bytes']/1e9:8.1f} GB  {r['path']}")
