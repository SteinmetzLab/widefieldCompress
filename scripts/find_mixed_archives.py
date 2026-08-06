"""How many Y: archives bundle non-frame content alongside the widefield frames?

Cheap test: an archive of equally-sized frames has a valid tar header at every computed offset.
If the header at the *last* computed member offset is not a valid header, the constant stride
breaks somewhere, which for these archives means other content got tar'd in - typically a whole
SpikeGLX recording. Three reads per archive.

Read-only.
"""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wfcompress.tarwalk import BLOCK, MalformedArchive, parse_size

HERE = Path(__file__).resolve().parents[1]
OUT = HERE / "data" / "mixed_archives.csv"


def looks_like_header(h: bytes) -> bool:
    return len(h) == BLOCK and h.strip(b"\0") != b"" and h[257:263] in (b"ustar\x00", b"ustar ")


def probe(row):
    path, size = Path(row["path"]), int(row["bytes"])
    out = {"path": str(path), "bytes": size, "uniform": "", "n_members": "",
           "break_at_member": "", "first_other": "", "error": ""}
    try:
        with open(path, "rb") as fh:
            h0 = fh.read(BLOCK)
            if not looks_like_header(h0):
                out["error"] = "no tar header at offset 0"
                return out
            lead = BLOCK if parse_size(h0[124:136]) == 0 else 0
            fh.seek(lead)
            h1 = fh.read(BLOCK)
            if not looks_like_header(h1):
                out["error"] = "no tar header at the first member"
                return out
            msize = parse_size(h1[124:136])
            if msize == 0:
                out["error"] = "first member is empty"
                return out
            stride = BLOCK + ((msize + BLOCK - 1) // BLOCK) * BLOCK
            n = (size - lead) // stride
            out["n_members"] = n
            if n < 2:
                out["uniform"] = "True"
                return out
            fh.seek(lead + (n - 1) * stride)
            last = fh.read(BLOCK)
            if looks_like_header(last) and parse_size(last[124:136]) == msize:
                out["uniform"] = "True"
                return out

            out["uniform"] = "False"
            # bisect for the first offset that is not a frame header
            def ok(k):
                fh.seek(lead + k * stride)
                h = fh.read(BLOCK)
                try:
                    return looks_like_header(h) and parse_size(h[124:136]) == msize
                except MalformedArchive:
                    return False

            lo, hi = 0, n - 1
            if not ok(0):
                out["break_at_member"] = 0
                return out
            while hi - lo > 1:
                mid = (lo + hi) // 2
                (lo, hi) = (mid, hi) if ok(mid) else (lo, mid)
            out["break_at_member"] = lo + 1
            # Walk forward from the last member that is definitely at a computed offset. The
            # stride only breaks once something non-frame appears, so a short sequential walk
            # from here names it, without walking the whole archive.
            off = lead + lo * stride
            names = []
            for _ in range(6):
                fh.seek(off)
                h = fh.read(BLOCK)
                if not looks_like_header(h):
                    names.append("<not a tar header>")
                    break
                try:
                    sz = parse_size(h[124:136])
                except MalformedArchive:
                    names.append("<unparseable size>")
                    break
                nm = h[:100].rstrip(b"\0").decode("ascii", "backslashreplace")
                names.append(f"{nm} ({sz:,} B)")
                off += BLOCK + ((sz + BLOCK - 1) // BLOCK) * BLOCK
            out["first_other"] = " | ".join(names[1:]) or names[0]
    except (OSError, MalformedArchive) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


prev = HERE / "data" / "mixed_archives.csv"
only_mixed = prev.exists() and "--all" not in __import__("sys").argv
if only_mixed:
    keep = {r["path"] for r in csv.DictReader(prev.open(encoding="utf-8"))
            if r["uniform"] == "False"}
    rows = [r for r in csv.DictReader((HERE / "data" / "census_Y.csv").open(encoding="utf-8"))
            if r["path"] in keep]
    OUT = HERE / "data" / "mixed_archives_detail.csv"
else:
    rows = [r for r in csv.DictReader((HERE / "data" / "census_Y.csv").open(encoding="utf-8"))
            if r["kind"] in ("frame-N", "basler-tiff") and int(r["bytes"]) > 0]
print(f"probing {len(rows)} archives ...", flush=True)
with ThreadPoolExecutor(6) as ex:
    out = list(ex.map(probe, rows))

with OUT.open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=list(out[0].keys()))
    w.writeheader()
    w.writerows(out)

mixed = [r for r in out if r["uniform"] == "False"]
errs = [r for r in out if r["error"]]
uni = [r for r in out if r["uniform"] == "True"]
tot = sum(r["bytes"] for r in out)
print(f"\nuniform frame archives : {len(uni):5d}  {sum(r['bytes'] for r in uni)/1e12:6.2f} TB")
print(f"mixed content          : {len(mixed):5d}  {sum(r['bytes'] for r in mixed)/1e12:6.2f} TB")
print(f"probe errors           : {len(errs):5d}  {sum(r['bytes'] for r in errs)/1e12:6.2f} TB")
print(f"total                  : {len(out):5d}  {tot/1e12:6.2f} TB")

if mixed:
    print("\nmixed archives - frames, then something else:")
    for r in sorted(mixed, key=lambda r: -r["bytes"]):
        frac = (r["break_at_member"] or 0) / max(r["n_members"], 1)
        print(f"  {r['bytes']/1e9:7.1f} GB  frames up to #{r['break_at_member']:,} "
              f"of {r['n_members']:,} ({100*frac:.0f}%)  then {r['first_other'][:52]!r}")
for r in errs[:10]:
    print(f"  ERROR {r['error'][:60]}  {r['path']}")
print(f"\nwrote {OUT}")
