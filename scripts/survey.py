"""Read-only survey: is every widefield.tar the same shape of thing? Reads ~1 KB per tar."""

import os
import random
from pathlib import Path

ROOTS = {"Y": Path(r"Y:\Subjects"), "Z": Path("Z:/")}

paths = []
for tag, listing in (("Y", "tars_Y.txt"), ("Z", "tars_Z.txt")):
    for line in Path(listing).read_text().splitlines():
        size, rel = line.split("\t")
        paths.append((tag, int(size), (ROOTS[tag] / rel.lstrip("./")).resolve()))

print(f"{len(paths)} tars, {sum(p[1] for p in paths)/1e12:.1f} TB total")
for tag in ("Y", "Z"):
    sub = [p for p in paths if p[0] == tag]
    print(f"  {tag}: {len(sub):5d} files  {sum(s for _, s, _ in sub)/1e12:6.1f} TB")

random.seed(0)
sample = random.sample(paths, 25)

print("\nfirst-member header of 25 random tars:")
shapes = {}
for tag, size, p in sample:
    try:
        with open(p, "rb") as fh:
            h0 = fh.read(512)
            n0 = h0[:100].rstrip(b"\0").decode(errors="replace")
            s0 = int(h0[124:136].rstrip(b"\0 ").decode() or "0", 8)
            typ0 = chr(h0[156])
            # if the first entry is a directory, the real payload is the next header
            if s0 == 0:
                h1 = fh.read(512)
                n1 = h1[:100].rstrip(b"\0").decode(errors="replace")
                s1 = int(h1[124:136].rstrip(b"\0 ").decode() or "0", 8)
            else:
                n1, s1 = n0, s0
        stride = 512 + ((s1 + 511) // 512) * 512
        nframes = (size - 512) // stride if stride else 0
        px = s1 // 2
        side = int(px**0.5)
        shape = f"{side}x{side}" if side * side == px else f"{px}px"
        shapes.setdefault((s1, shape), []).append(p)
        print(f"  {tag} {size/1e9:7.1f} GB  first={n0!r:22s} member={n1!r:20s} "
              f"{s1:>9,d} B  {shape:>10s}  ~{nframes:,d} frames")
    except OSError as e:
        print(f"  {tag} {p}: {e}")

print("\ndistinct member sizes seen:")
for (s, shape), ps in sorted(shapes.items()):
    print(f"  {s:>10,d} B  {shape:>10s}  n={len(ps)}")

print("\nper-session companions (does the SVD output already exist alongside?):")
for tag, size, p in sample[:8]:
    d = p.parent
    have = [x for x in ("blue", "violet", "corr") if (d / x).is_dir()]
    svd = (d / "blue" / "svdSpatialComponents.npy")
    print(f"  {d}  channels={have or 'NONE'}  blueSVD={'yes' if svd.exists() else 'NO'}")
