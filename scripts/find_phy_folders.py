"""Inventory `.phy` folders on the lab share.

Phy writes a `.phy` cache directory beside a spike sorting. It is scratch data that is meant to
stay on the acquisition or analysis machine, but it sometimes gets copied to the server along with
the sorting output.

Read-only. This produces a CSV and deletes nothing.

    python scripts/find_phy_folders.py                     # Y:\\Subjects
    python scripts/find_phy_folders.py --root Z:\\Subjects_OLD --out data/phy_Z.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

TARGET = ".phy"
FIELDS = [
    "path", "subject", "date", "exp", "sorter_dir",
    "size_bytes", "n_files", "newest_mtime_utc", "oldest_mtime_utc", "error",
]


def measure(folder: str) -> tuple[int, int, float, float]:
    """Total bytes, file count, and mtime range of everything under ``folder``."""
    total = count = 0
    newest, oldest = 0.0, float("inf")
    stack = [folder]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            st = e.stat(follow_symlinks=False)
                            total += st.st_size
                            count += 1
                            newest = max(newest, st.st_mtime)
                            oldest = min(oldest, st.st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count, newest, (0.0 if oldest == float("inf") else oldest)


def find_phy(root: str, errors: list[str]) -> list[str]:
    """Every directory named `.phy` under ``root``. Does not descend into one once found."""
    found, stack = [], [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if not e.is_dir(follow_symlinks=False):
                            continue
                    except OSError as exc:
                        errors.append(f"{e.path}: {exc}")
                        continue
                    if e.name == TARGET:
                        found.append(e.path)   # do not descend; we want the folder, not its guts
                    else:
                        stack.append(e.path)
        except OSError as exc:
            errors.append(f"{d}: {exc}")
    return found


def session_parts(p: Path, root: Path) -> tuple[str, str, str, str]:
    """subject / date / exp / the directory the .phy sits in, as far as they can be identified."""
    try:
        rel = p.relative_to(root).parts
    except ValueError:
        rel = p.parts
    subject = rel[0] if len(rel) > 0 else ""
    date = rel[1] if len(rel) > 1 else ""
    exp = rel[2] if len(rel) > 2 else ""
    return subject, date, exp, str(p.parent)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=r"\\sahale.biostr.washington.edu\data\Subjects")
    ap.add_argument("--out", default="data/phy_folders.csv")
    ap.add_argument("--workers", type=int, default=6,
                    help="kept modest so this does not fight the compression run for SMB "
                         "round trips")
    args = ap.parse_args()

    root = Path(args.root)
    print(f"scanning {root} for {TARGET} folders ...", flush=True)
    t0 = time.perf_counter()
    errors: list[str] = []

    # fan out over the top level so one slow subject does not serialise the whole walk
    try:
        tops = [e.path for e in os.scandir(root) if e.is_dir()]
    except OSError as e:
        print(f"cannot read {root}: {e}")
        return 1

    with ThreadPoolExecutor(args.workers) as pool:
        found = [p for part in pool.map(lambda d: find_phy(d, errors), tops) for p in part]
    print(f"  walk took {(time.perf_counter()-t0)/60:.1f} min; {len(found)} {TARGET} folders",
          flush=True)

    with ThreadPoolExecutor(args.workers) as pool:
        sizes = list(pool.map(measure, found))

    rows = []
    for path, (total, count, newest, oldest) in zip(found, sizes, strict=True):
        p = Path(path)
        subject, date, exp, parent = session_parts(p, root)
        rows.append({
            "path": path, "subject": subject, "date": date, "exp": exp,
            "sorter_dir": parent, "size_bytes": total, "n_files": count,
            "newest_mtime_utc": iso(newest), "oldest_mtime_utc": iso(oldest), "error": "",
        })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: -r["size_bytes"]))

    tot = sum(r["size_bytes"] for r in rows)
    print(f"\n{len(rows)} {TARGET} folders, {tot/1e9:.1f} GB, "
          f"{sum(r['n_files'] for r in rows):,} files")
    if rows:
        print("\nlargest:")
        for r in sorted(rows, key=lambda r: -r["size_bytes"])[:20]:
            print(f"  {r['size_bytes']/1e9:8.2f} GB  {r['n_files']:>7,d} files  "
                  f"{r['newest_mtime_utc'][:10]}  {r['path']}")
    if errors:
        print(f"\n{len(errors)} directories could not be read (the list may be incomplete):")
        for e in errors[:10]:
            print(f"  {e}")
    print(f"\nwrote {out}")
    print("nothing was deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
