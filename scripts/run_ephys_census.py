"""Walk Y: for raw SpikeGLX .bin files and report how much compressing them would save.

Read-only. Writes data/ephys_census_Y.csv and prints a summary.
"""

from __future__ import annotations

import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from wfcompress.lab.ephys_census import scan

HERE = Path(__file__).resolve().parents[1]
ROOT = Path(r"\\sahale.biostr.washington.edu\data\Subjects")
OUT = HERE / "data" / "ephys_census_Y.csv"


def main() -> int:
    t0 = time.perf_counter()
    print(f"walking {ROOT} ...", flush=True)

    def report(depth, n_dirs, n_next, n_files):
        print(f"  depth {depth}: listed {n_dirs:,} dirs -> {n_next:,} subdirs, "
              f"{n_files:,} files so far  [{time.perf_counter()-t0:.0f} s]", flush=True)

    census, errors = scan(ROOT, progress=report)
    print(f"  {len(census.records):,} raw .bin files in {time.perf_counter()-t0:.0f} s")
    if errors:
        print(f"  {len(errors)} unreadable directories:")
        for e in errors[:10]:
            print(f"    {e}")

    census.write_csv(OUT)
    rs = census.records
    total = sum(r.bytes for r in rs)
    print(f"\ntotal raw ephys: {total/1e12:.2f} TB across {len(rs):,} files")

    print("\nby band:")
    by_band = defaultdict(list)
    for r in rs:
        by_band[r.band or "?"].append(r)
    for band, group in sorted(by_band.items(), key=lambda kv: -sum(x.bytes for x in kv[1])):
        b = sum(x.bytes for x in group)
        print(f"  {band:>5}  {len(group):5,d} files  {b/1e12:7.2f} TB  ({100*b/total:4.1f}%)")

    already = [r for r in rs if r.has_cbin]
    if already:
        ab = sum(r.bytes for r in already)
        cb = sum(r.cbin_bytes for r in already)
        print(f"\nalready have a .cbin beside them: {len(already):,} files, {ab/1e12:.2f} TB raw")
        if cb:
            print(f"  those .cbin total {cb/1e12:.2f} TB  -> observed ratio x{ab/cb:.2f}")
        print("  (raw + compressed both present: deleting the raw is a saving already banked)")

    todo = [r for r in rs if not r.has_cbin]
    tb = sum(r.bytes for r in todo)
    print(f"\nnot yet compressed: {len(todo):,} files, {tb/1e12:.2f} TB")
    for ratio in (2.0, 2.5, 3.0):
        print(f"  at x{ratio:.1f}  ->  {tb/ratio/1e12:5.2f} TB kept, "
              f"{tb*(1-1/ratio)/1e12:5.2f} TB reclaimed")

    print("\nmissing metadata (mtscomp needs nSavedChans and the sample rate):")
    no_meta = [r for r in todo if not r.has_meta]
    print(f"  {len(no_meta):,} of {len(todo):,} files have no .meta "
          f"({sum(r.bytes for r in no_meta)/1e12:.2f} TB)")

    bad = [r for r in rs if r.error]
    if bad:
        print(f"\n{len(bad)} files errored:")
        for k, v in Counter(r.error[:60] for r in bad).most_common(5):
            print(f"  {v:4d}  {k}")

    print("\nbiggest 10:")
    for r in sorted(rs, key=lambda r: -r.bytes)[:10]:
        print(f"  {r.bytes/1e9:8.1f} GB  {r.band:>4}  {r.subject}/{r.date}/{r.session}"
              f"{'  [has .cbin]' if r.has_cbin else ''}")

    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
