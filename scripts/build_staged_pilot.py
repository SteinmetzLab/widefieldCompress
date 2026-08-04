"""Census for the staged pilot.

Composition, deliberately:

* the nine sessions from the first pilot, so their format-v1 .wfz files get replaced with v2
  (v1 lacks the acquisition-order map);
* the largest archive in the corpus, 430 GB / ~680k frames, which stresses the memory behaviour
  the code review flagged and is the only size class not yet exercised;
* three archives that hold no signal, which the lab wants compressed rather than deleted; these
  are also headerless with no meanImage.npy, so they exercise --assume-shape.
"""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wfcompress.lab.census import CSV_FIELDS, probe

HERE = Path(__file__).resolve().parents[1]
OUT = HERE / "data" / "staged_pilot_census.csv"

FIRST_PILOT = [
    r"Y:\Subjects\test\2026-02-17\1\widefield.tar",
    r"Y:\Subjects\ZYE_0035\2021-07-17\1\widefield.tar",
    r"Y:\Subjects\AL_0033\2025-03-17\1\widefield.tar",
    r"Y:\Subjects\AL_0048\2026-06-11\4\widefield.tar",
    r"Y:\Subjects\test\2025-11-05\1\1.tar",          # failed first time: no geometry
    r"Y:\Subjects\ZYE_0008\2020-07-25\1\widefield.tar",
    r"Y:\Subjects\SM_0001\2020-09-03\1\widefield.tar",
    r"Y:\Subjects\AL_0023\2023-05-15\5\widefield.tar",
    r"Y:\Subjects\ZYE_0007\2020-07-29\1\widefield.tar",
]

LARGEST = [r"Y:\Subjects\ZYE_0046\2021-11-27\2\widefield.tar"]

NO_SIGNAL = [
    r"Y:\Subjects\AL_0033\2025-02-24\3\3.tar",
    r"Y:\Subjects\test\2025-11-04\1.tar",
    r"Y:\Subjects\default\2025-03-05\3\widefield.tar",
]

paths = FIRST_PILOT + LARGEST + NO_SIGNAL
with ThreadPoolExecutor(12) as ex:
    recs = list(ex.map(lambda p: probe(p, "Y"), paths))

with OUT.open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
    w.writeheader()
    for r in recs:
        w.writerow(vars(r))

total = sum(r.bytes for r in recs)
print(f"{len(recs)} sessions, {total/1e9:.1f} GB\n")
for r in sorted(recs, key=lambda r: -r.bytes):
    geom = f"{r.rows}x{r.cols}" if r.rows else "UNKNOWN (needs --assume-shape)"
    print(f"  {r.bytes/1e9:8.1f} GB  {r.kind:12s} {geom:>30s}  shift={r.shift:2d}  "
          f"{r.est_frames:>8,d} frames  {Path(r.path).parent}")
print(f"\nwrote {OUT}")
