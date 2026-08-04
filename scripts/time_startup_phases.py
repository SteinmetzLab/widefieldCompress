"""Where does a large archive spend its time before the first frame is encoded?

compress() makes three passes over the source before it writes anything: read_entries() to get the
headers, _preflight() to check padding and sizes, and _detect_shift() to sample frames. Each of the
first two touches every member with a small scattered read, which is cheap alone and may not be
cheap with eight workers competing for the share.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from wfcompress import codec, frames, tarwalk

TARGET = Path(sys.argv[1] if len(sys.argv) > 1
              else r"Y:\Subjects\AB_0032\2024-04-16\1\widefield.tar")
print(f"{TARGET}\n  {TARGET.stat().st_size/1e9:.1f} GB")

t0 = time.perf_counter()
entries = tarwalk.read_entries(TARGET)
t_entries = time.perf_counter() - t0
members = [e for e in entries if e.size > 0]
print(f"  read_entries   : {t_entries:7.1f} s   {len(members):,} members "
      f"({1e3*t_entries/max(len(members),1):.3f} ms each)")

with open(TARGET, "rb") as fh:
    fh.seek(members[0].data_offset)
    layout = frames.detect_layout(fh.read(members[0].size), None)

t0 = time.perf_counter()
codec._preflight(TARGET, entries, members)
t_pre = time.perf_counter() - t0
print(f"  _preflight     : {t_pre:7.1f} s   ({1e3*t_pre/max(len(members),1):.3f} ms per member)")

t0 = time.perf_counter()
shift, payload = codec._detect_shift(TARGET, members, layout)
t_shift = time.perf_counter() - t0
print(f"  _detect_shift  : {t_shift:7.1f} s   shift={shift}")

total = t_entries + t_pre + t_shift
print(f"\n  total before the first frame is encoded: {total/60:.1f} min")
print(f"  with 8 workers all doing this at once over one SMB link, expect worse than 8x")
print(f"  (was 424 s before headers were fetched concurrently and the padding check")
print(f"   moved into the encoding pass, which reads those bytes anyway)")
