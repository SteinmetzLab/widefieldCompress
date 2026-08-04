"""Re-scan the Y: share and write a census in the current schema.

The checked-in `data/tar_census.csv` predates the Census dataclass and uses an older column set
(`tag` rather than `server`, no geometry/shift/SVD/error fields), so `Census.read_csv` cannot read
it and the batch driver cannot be pointed at it. Run this to produce one that it can.

Re-run immediately before any bulk job: the census is a snapshot and new sessions land
continuously.
"""

from __future__ import annotations

import sys
from pathlib import Path

from wfcompress.lab.census import DEFAULT_ROOTS, scan

OUT = Path(__file__).resolve().parents[1] / "data" / "census_Y.csv"

roots = {"Y": DEFAULT_ROOTS["Y"]}
print(f"scanning {roots['Y']} ...")
census = scan(roots=roots, strict=False)
census.write_csv(OUT)

wf = census.widefield
print(f"{len(census.records)} tars found, {len(wf)} widefield, "
      f"{sum(r.bytes for r in wf)/1e12:.1f} TB")

errs = [r for r in census.records if r.error]
if errs:
    print(f"\n{len(errs)} archives could not be fully probed:")
    for r in errs[:15]:
        print(f"  {r.bytes/1e9:8.1f} GB  {r.error[:70]}  {r.path}")

blocked = [r for r in wf if r.rows == 0]
if blocked:
    print(f"\n{len(blocked)} widefield archives have no recoverable geometry "
          f"({sum(r.bytes for r in blocked)/1e12:.2f} TB)")

print(f"\nwrote {OUT}")
if not wf:
    sys.exit("no widefield archives found - is the share mounted?")
