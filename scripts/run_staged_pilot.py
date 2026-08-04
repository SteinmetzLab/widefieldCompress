"""Run the staged pilot in two stages, because they measure different things.

Stage A: the twelve smaller sessions at the recommended 8 jobs x 4 threads. This is the
arrangement the bulk run will use, so its aggregate rate is what the corpus estimate rests on.

Stage B: the single 430 GB archive on its own with 16 threads. One session cannot use 8 job slots,
so running it inside stage A would just leave most of the machine idle behind a long tail. On its
own it measures what the largest size class costs, and lets peak memory be observed -- the code
review flagged ~680k frames as a memory risk.

Nothing is deleted. Existing .wfz files from the first pilot are replaced in place, which the file
edit log records as `modify`.
"""

from __future__ import annotations

import csv
import subprocess
import sys
import time
from pathlib import Path

from wfcompress.lab.census import CSV_FIELDS

HERE = Path(__file__).resolve().parents[1]
DATA = HERE / "data"
PY = sys.executable
BIG = "ZYE_0046"

rows = list(csv.DictReader((DATA / "staged_pilot_census.csv").open(encoding="utf-8")))
small = [r for r in rows if BIG not in r["path"]]
big = [r for r in rows if BIG in r["path"]]


def write(name, subset):
    p = DATA / name
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(subset)
    return p, sum(int(r["bytes"]) for r in subset)


csv_a, bytes_a = write("pilot_stage_a.csv", small)
csv_b, bytes_b = write("pilot_stage_b.csv", big)

FILE_LOG = DATA / "fileEditLog.csv"
results = {}


def run(label, census, jobs, threads, log, nbytes):
    print(f"\n{'='*70}\n{label}: {nbytes/1e9:.1f} GB, {jobs} jobs x {threads} threads", flush=True)
    t0 = time.perf_counter()
    r = subprocess.run(
        [PY, "-m", "wfcompress.lab.batch", "--census", str(census), "--server", "Y",
         "--jobs", str(jobs), "--threads", str(threads),
         "--log", str(DATA / log), "--file-log", str(FILE_LOG),
         "--assume-shape", "560", "560"],
        text=True, capture_output=True,
    )
    dt = time.perf_counter() - t0
    print(r.stdout[-3000:], flush=True)
    if r.returncode:
        print("STDERR:", r.stderr[-2000:], flush=True)
    results[label] = (nbytes, dt, r.returncode)
    print(f"{label}: {dt/3600:.2f} h, {nbytes/1e6/dt:.1f} MB/s aggregate", flush=True)


run("stage A (12 sessions)", csv_a, 8, 4, "pilot_stage_a.jsonl", bytes_a)
run("stage B (430 GB archive)", csv_b, 1, 16, "pilot_stage_b.jsonl", bytes_b)

print(f"\n{'='*70}\nSTAGED PILOT SUMMARY")
tot_bytes = sum(v[0] for v in results.values())
tot_time = sum(v[1] for v in results.values())
for label, (nbytes, dt, rc) in results.items():
    print(f"  {label:28s} {nbytes/1e9:7.1f} GB  {dt/3600:5.2f} h  "
          f"{nbytes/1e6/dt:6.1f} MB/s  {'ok' if rc == 0 else 'SOME FAILED'}")
print(f"  {'total':28s} {tot_bytes/1e9:7.1f} GB  {tot_time/3600:5.2f} h  "
      f"{tot_bytes/1e6/tot_time:6.1f} MB/s")
