"""For each mixed archive, does the ephys it swallowed also exist unpacked on the server?

The 21 mixed `widefield.tar` files bundle a whole SpikeGLX recording behind the widefield frames.
Before deciding what to do with them we need to know whether that ephys is *only* inside the tar.
Two things count as "it exists elsewhere":

  * raw SpikeGLX still on disk - a ``.ap.bin`` or its compressed ``.ap.cbin`` form, anywhere under
    the session folder (or its siblings), of plausible size;
  * a spike sorting output derived from it (``spikes.times.npy`` and friends), which means the raw
    data was read at some point even if it has since been removed.

Read-only: lists directories, stats files, opens nothing.
"""

from __future__ import annotations

import csv
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
DETAIL = HERE / "data" / "mixed_archives_detail.csv"
OUT = HERE / "data" / "mixed_ephys_elsewhere.csv"

# The tar names the probe it holds, e.g. "1/p0_g0/p0_g0_imec0/p0_g0_t0.imec0.ap.bin".
BIN_RE = re.compile(r"([A-Za-z0-9_]+)_t\d+\.imec(\d+)\.ap\.bin \(([\d,]+) B\)")

EPHYS_SUFFIXES = (".ap.bin", ".ap.cbin", ".lf.bin", ".lf.cbin")
SORT_MARKERS = ("spikes.times.npy", "spikes.clusters.npy", "amplitudes.npy", "params.py")


def walk_shallow(root: Path, max_depth: int = 4):
    """Yield (path, is_dir, size) under root, stopping at max_depth. Tolerates missing dirs."""
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            try:
                is_dir = e.is_dir()
            except OSError:
                continue
            if is_dir:
                yield e, True, 0
                if depth + 1 < max_depth:
                    stack.append((e, depth + 1))
            else:
                try:
                    yield e, False, e.stat().st_size
                except OSError:
                    yield e, False, -1


def inspect(row: dict) -> dict:
    tar = Path(row["path"])
    session_num = tar.parent          # .../Subjects/FD_011/2026-02-25/1
    date_dir = session_num.parent     # .../Subjects/FD_011/2026-02-25

    m = BIN_RE.search(row["first_other"] or "")
    want_bytes = int(m.group(3).replace(",", "")) if m else 0
    want_stem = m.group(1) if m else ""

    out = {
        "path": str(tar),
        "tar_bytes": int(row["bytes"]),
        "ephys_in_tar_bytes": want_bytes,
        "ephys_in_tar_name": (m.group(0).split(" (")[0] if m else ""),
        "raw_ephys_outside": "",
        "raw_ephys_outside_bytes": 0,
        "raw_ephys_outside_paths": "",
        "sorting_outside": "",
        "sorting_paths": "",
        "session_dirs": "",
        "error": "",
    }
    try:
        if not date_dir.exists():
            out["error"] = "session date folder missing"
            return out

        raw_hits, sort_hits, top_dirs = [], [], []
        for e, is_dir, size in walk_shallow(date_dir, max_depth=5):
            name = e.name
            if is_dir:
                if e.parent == date_dir or e.parent == session_num:
                    top_dirs.append(str(e.relative_to(date_dir)))
                continue
            if name.endswith(EPHYS_SUFFIXES):
                raw_hits.append((str(e.relative_to(date_dir)), size))
            elif name in SORT_MARKERS:
                sort_hits.append(str(e.relative_to(date_dir)))

        out["raw_ephys_outside"] = "True" if raw_hits else "False"
        out["raw_ephys_outside_bytes"] = sum(s for _, s in raw_hits if s > 0)
        out["raw_ephys_outside_paths"] = " | ".join(
            f"{p} ({s:,} B)" for p, s in sorted(raw_hits, key=lambda x: -x[1])[:6]
        )
        out["sorting_outside"] = "True" if sort_hits else "False"
        out["sorting_paths"] = " | ".join(sorted(sort_hits)[:6])
        out["session_dirs"] = " | ".join(sorted(set(top_dirs))[:20])
        # does the stem the tar names appear anywhere outside?
        if want_stem:
            out["stem_found_outside"] = str(
                any(want_stem in p for p, _ in raw_hits) or any(want_stem in p for p in sort_hits)
            )
    except OSError as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


rows = list(csv.DictReader(DETAIL.open(encoding="utf-8")))
print(f"inspecting {len(rows)} mixed-archive sessions ...", flush=True)
with ThreadPoolExecutor(8) as ex:
    results = list(ex.map(inspect, rows))

fields = ["path", "tar_bytes", "ephys_in_tar_bytes", "ephys_in_tar_name", "raw_ephys_outside",
          "raw_ephys_outside_bytes", "raw_ephys_outside_paths", "stem_found_outside",
          "sorting_outside", "sorting_paths", "session_dirs", "error"]
with OUT.open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(results)

print()
only_in_tar = [r for r in results if r["raw_ephys_outside"] == "False"]
elsewhere = [r for r in results if r["raw_ephys_outside"] == "True"]
print(f"raw ephys ALSO on disk outside the tar : {len(elsewhere)}")
print(f"raw ephys ONLY inside the tar          : {len(only_in_tar)}")
print(f"  of those, a spike sorting exists     : "
      f"{sum(1 for r in only_in_tar if r['sorting_outside'] == 'True')}")
print()
for r in sorted(results, key=lambda r: -r["tar_bytes"]):
    tag = "OUTSIDE-TOO" if r["raw_ephys_outside"] == "True" else "TAR-ONLY   "
    sort = "sorted" if r["sorting_outside"] == "True" else "no-sort"
    p = Path(r["path"])
    print(f"  {tag} {sort:7s} {r['tar_bytes']/1e9:7.1f} GB tar  "
          f"{r['ephys_in_tar_bytes']/1e9:6.1f} GB ephys inside   "
          f"{p.parts[-4]}/{p.parts[-3]}/{p.parts[-2]}")
    if r["raw_ephys_outside_paths"]:
        print(f"      outside: {r['raw_ephys_outside_paths'][:150]}")
    if r["sorting_paths"]:
        print(f"      sorting: {r['sorting_paths'][:150]}")
    if r["error"]:
        print(f"      ERROR: {r['error']}")

print(f"\nwrote {OUT}")
