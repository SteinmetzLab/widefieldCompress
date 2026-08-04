"""Find the best --jobs / --threads split for the bulk run, using the real batch driver.

Measures the full realistic workload: compress plus streaming verification, reading from and
writing to the share.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wfcompress.lab.census import CSV_FIELDS, probe

HERE = Path(__file__).resolve().parents[1]
SCRATCH = Path(r"Y:\temp\wfc-bench")
MINI = HERE / "data" / "sweep_census.csv"
PY = sys.executable

CANDIDATES = [
    r"Y:\Subjects\ZYE_0031\2021-04-13\3\widefield.tar",
    r"Y:\Subjects\LK_0001\2021-04-13\5\widefield.tar",
    r"Y:\Subjects\LK_0001\2021-04-13\3\widefield.tar",
    r"Y:\Subjects\ZYE_0031\2021-04-13\5\widefield.tar",
    r"Y:\Subjects\LK_0001\2021-04-22\5\widefield.tar",
    r"Y:\Subjects\ZYE_0031\2021-04-22\3\widefield.tar",
    r"Y:\Subjects\LK_0001\2021-04-22\1\widefield.tar",
    r"Y:\Subjects\ZYE_0031\2021-04-22\1\widefield.tar",
]

if not MINI.exists():
    with ThreadPoolExecutor(8) as ex:
        recs = list(ex.map(lambda p: probe(p, "Y"), CANDIDATES))
    with MINI.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in recs:
            w.writerow(vars(r))

total = sum(Path(p).stat().st_size for p in CANDIDATES)
print(f"{len(CANDIDATES)} sessions, {total/1e9:.2f} GB, compress + streaming verify\n")
print(f"{'jobs':>5s}{'threads':>9s}{'workers':>9s}{'min':>8s}{'MB/s':>9s}{'vs 1x8':>9s}")
print("-" * 50)

baseline = None
for jobs, threads in [(1, 8), (1, 16), (4, 4), (4, 8), (6, 4), (8, 2), (8, 4)]:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    log = SCRATCH / "log.jsonl"
    t0 = time.perf_counter()
    r = subprocess.run(
        [PY, "-m", "wfcompress.lab.batch", "--census", str(MINI), "--server", "Y",
         "--out-dir", str(SCRATCH), "--log", str(log),
         "--jobs", str(jobs), "--threads", str(threads)],
        capture_output=True, text=True,
    )
    dt = time.perf_counter() - t0
    if r.returncode:
        print(f"{jobs:>5d}{threads:>9d}  FAILED\n{r.stdout[-600:]}\n{r.stderr[-600:]}")
        continue
    rate = total / 1e6 / dt
    if baseline is None:
        baseline = rate
    print(f"{jobs:>5d}{threads:>9d}{jobs*threads:>9d}{dt/60:>8.2f}{rate:>9.1f}"
          f"{rate/baseline:>8.2f}x")

shutil.rmtree(SCRATCH, ignore_errors=True)
print("\nY: corpus is 120.7 TB; at the best rate above that is "
      f"{120.7e12/1e6/ (baseline or 1) /86400:.0f} days at baseline")
