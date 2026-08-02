"""Read-only census of every widefield.tar: member format, member size, frame count.

Reads ~2 KB from the head of each tar. Writes one local CSV. Nothing is written to Y: or Z:.
"""

import csv
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOTS = {"Y": Path(r"Y:\Subjects"), "Z": Path("Z:/")}
OUT = Path(r"D:\Dropbox\code\widefieldCompress\tar_census.csv")

paths = []
for tag, listing in (("Y", "tars_Y.txt"), ("Z", "tars_Z.txt")):
    for line in Path(listing).read_text().splitlines():
        size, rel = line.split("\t")
        paths.append((tag, int(size), ROOTS[tag] / rel.lstrip("./")))


def classify(name: str) -> str:
    if re.fullmatch(r".*/frame-\d+", name):
        return "frame-N"
    if "Basler" in name and name.endswith((".tif", ".tiff")):
        return "basler-tiff"
    if name.endswith(".ome.tif"):
        return "ome-tif"
    if name.endswith((".tif", ".tiff")):
        return "other-tiff"
    return "other"


def probe(rec):
    tag, size, p = rec
    try:
        with open(p, "rb") as fh:
            head = fh.read(1536)
    except OSError as e:
        return dict(tag=tag, path=str(p), bytes=size, kind=f"ERROR:{type(e).__name__}")

    def hdr(off):
        h = head[off : off + 512]
        if len(h) < 512 or h[:1] == b"\0":
            return None, 0
        nm = h[:100].rstrip(b"\0").decode("utf-8", "replace")
        sz = int(h[124:136].rstrip(b"\0 ").decode(errors="replace") or "0", 8)
        return nm, sz

    n0, s0 = hdr(0)
    first = n0
    if s0 == 0:  # leading directory entry
        n1, s1 = hdr(512)
    else:
        n1, s1 = n0, s0
    if not n1:
        return dict(tag=tag, path=str(p), bytes=size, kind="EMPTY")

    stride = 512 + ((s1 + 511) // 512) * 512
    return dict(
        tag=tag,
        path=str(p),
        bytes=size,
        kind=classify(n1),
        member_bytes=s1,
        est_frames=(size - (512 if s0 == 0 else 0)) // stride,
        first_entry=first,
        first_member=n1,
    )


with ThreadPoolExecutor(max_workers=16) as ex:
    rows = list(ex.map(probe, paths))

cols = ["tag", "path", "bytes", "kind", "member_bytes", "est_frames", "first_entry", "first_member"]
with OUT.open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)

from collections import Counter, defaultdict

kinds = Counter(r["kind"] for r in rows)
bykind = defaultdict(int)
for r in rows:
    bykind[r["kind"]] += r["bytes"]
print("format census:")
for k, n in kinds.most_common():
    print(f"  {k:20s} n={n:5d}   {bykind[k]/1e12:7.2f} TB")

sizes = Counter((r["kind"], r.get("member_bytes")) for r in rows if r.get("member_bytes"))
print("\nmember sizes:")
for (k, s), n in sorted(sizes.items(), key=lambda x: -x[1]):
    print(f"  {k:14s} {s:>12,d} B   n={n:5d}")

print(f"\nwrote {OUT}")
