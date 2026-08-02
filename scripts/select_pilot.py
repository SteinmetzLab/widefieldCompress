"""Pick 10 diverse sessions for the Phase 1 pilot and write a census CSV for the batch driver.

Diversity axes that matter: flavour (frame-N vs basler-tiff), frame geometry (including a
non-square one), bit shift (0 and 4), acquisition era, and size.
"""

from __future__ import annotations

import csv
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wfcompress.lab.census import CSV_FIELDS, probe

HERE = Path(__file__).resolve().parents[1]
OLD = HERE / "data" / "tar_census.csv"
OUT = HERE / "data" / "pilot_census.csv"

rows = [r for r in csv.DictReader(OLD.open(encoding="utf-8"))]
cands = [
    r for r in rows
    if r["tag"] == "Y"
    and r["kind"] in ("frame-N", "basler-tiff", "other")
    and 1e9 < int(r["bytes"]) < 40e9          # small enough to pilot quickly
]
print(f"{len(cands)} candidate tars on Y between 1 and 40 GB")

# probe a spread of them so we can select on real geometry / shift, not guesses
random.seed(7)
sample = random.sample(cands, min(70, len(cands)))
# make sure the frame-N flavour is represented -- it is only ~6% of the corpus
frame_n = [r for r in cands if r["kind"] in ("frame-N", "other")]
sample += random.sample(frame_n, min(25, len(frame_n)))
seen, uniq = set(), []
for r in sample:
    if r["path"] not in seen:
        seen.add(r["path"])
        uniq.append(r)
print(f"probing {len(uniq)}...")

with ThreadPoolExecutor(12) as ex:
    probed = list(ex.map(lambda r: probe(r["path"], "Y"), uniq))

good = [p for p in probed if p.kind in ("frame-N", "basler-tiff") and not p.error and p.rows]
bad = [p for p in probed if p.error]
print(f"{len(good)} probed cleanly, {len(bad)} with errors")
for p in bad[:6]:
    print(f"   ! {p.error}   {p.path}")

buckets = defaultdict(list)
for p in good:
    buckets[(p.kind, (p.rows, p.cols), p.shift)].append(p)
print(f"\n{len(buckets)} distinct (flavour, geometry, shift) combinations:")
for k, v in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
    print(f"   {k[0]:12s} {str(k[1]):>12s} shift={k[2]}  n={len(v)}")

# one from each combination, smallest first, capped at 10 and at a sane total size
picks, total = [], 0
for _, group in sorted(buckets.items(), key=lambda kv: min(p.bytes for p in kv[1])):
    group.sort(key=lambda p: p.bytes)
    for p in group[:1]:
        if len(picks) < 10 and total + p.bytes < 120e9:
            picks.append(p)
            total += p.bytes
# top up with the next-smallest unused, preferring under-represented flavours
if len(picks) < 10:
    rest = sorted((p for p in good if p not in picks), key=lambda p: p.bytes)
    rest.sort(key=lambda p: (p.kind != "frame-N", p.bytes))
    for p in rest:
        if len(picks) < 10 and total + p.bytes < 120e9:
            picks.append(p)
            total += p.bytes

print(f"\nselected {len(picks)} sessions, {total/1e9:.1f} GB total:")
for p in picks:
    print(f"   {p.bytes/1e9:7.2f} GB  {p.kind:12s} {p.rows}x{p.cols:<5d} shift={p.shift} "
          f"payload={p.payload_bits:2d}  svd={'y' if p.has_svd else 'n'}  {p.path}")

with OUT.open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
    w.writeheader()
    for p in picks:
        w.writerow(vars(p))
print(f"\nwrote {OUT}")
