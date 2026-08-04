"""How long does it take just to enumerate a large archive's tar headers over SMB?

read_entries() does one seek + 512-byte read per member. On a 346 GB archive that is ~550,000
round trips to the share, and _preflight() then does another one per member to check the padding.
If that is where the time goes, both passes are worth restructuring.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from wfcompress import tarwalk

TARGET = Path(sys.argv[1] if len(sys.argv) > 1
              else r"Y:\Subjects\AB_0032\2024-04-16\1\widefield.tar")

size = TARGET.stat().st_size
print(f"{TARGET}\n  {size/1e9:.1f} GB")

# how fast is a plain sequential read of the same file, for comparison?
t0 = time.perf_counter()
read = 0
with open(TARGET, "rb") as fh:
    while read < 512 * 1024 * 1024:  # 512 MB is enough to establish the rate
        chunk = fh.read(8 << 20)
        if not chunk:
            break
        read += len(chunk)
dt = time.perf_counter() - t0
print(f"  bulk sequential read : {read/1e6/dt:7.1f} MB/s  (8 MB reads)")

# now the header walk, sampled: time the first N members and extrapolate
N = 20000
t0 = time.perf_counter()
count = 0
with open(TARGET, "rb") as fh:
    for _e in tarwalk.walk(fh):
        count += 1
        if count >= N:
            break
dt = time.perf_counter() - t0
per = dt / count
est_members = size // 632320  # basler-tiff stride
print(f"  header walk          : {count} members in {dt:.1f} s = {per*1e3:.2f} ms each")
print(f"  extrapolated to ~{est_members:,} members: {per*est_members/60:.1f} min")
print(f"  and _preflight does another pass of the same shape: "
      f"{2*per*est_members/60:.1f} min before a single frame is encoded")
