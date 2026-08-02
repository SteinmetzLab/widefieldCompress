"""Read-only: what exactly is inside a Basler-TIFF-flavoured widefield.tar?"""

import io

import numpy as np
import tifffile

TAR = r"Y:\Subjects\ZYE_0095\2025-07-12\3\widefield.tar"
BLOCK = 512


def members(fh, n):
    """Yield (name, size, data_offset) for the first n real members."""
    off = 0
    got = 0
    while got < n:
        fh.seek(off)
        h = fh.read(BLOCK)
        if not h or h[:1] == b"\0":
            return
        name = h[:100].rstrip(b"\0").decode(errors="replace")
        size = int(h[124:136].rstrip(b"\0 ").decode(errors="replace") or "0", 8)
        if size:
            yield name, size, off + BLOCK
            got += 1
        off += BLOCK + ((size + 511) // 512) * 512


with open(TAR, "rb") as fh:
    ms = list(members(fh, 6))
    for nm, sz, off in ms:
        print(f"  {sz:>9,d} B  {nm}")

    nm, sz, off = ms[0]
    fh.seek(off)
    raw = fh.read(sz)

print(f"\nfirst member: {sz:,d} bytes, magic={raw[:4]!r}")
t = tifffile.TiffFile(io.BytesIO(raw))
p = t.pages[0]
print(f"pages: {len(t.pages)}")
print(f"shape {p.shape}  dtype {p.dtype}  compression {p.compression}")
print(f"photometric {p.photometric}  bitspersample {p.bitspersample}")
print(f"strips: {len(p.dataoffsets)}  dataoffsets[0]={p.dataoffsets[0]}  "
      f"bytecounts={p.databytecounts[:3]}")
pixel_bytes = sum(p.databytecounts)
print(f"pixel payload {pixel_bytes:,d} B   header/metadata overhead {sz - pixel_bytes:,d} B "
      f"({100*(sz-pixel_bytes)/sz:.2f}%)")

print("\ntags:")
for k, v in p.tags.items():
    s = str(v.value)
    print(f"  {k:28s} = {s[:90]}{'...' if len(s) > 90 else ''}")

a = p.asarray()
print(f"\narray {a.shape} {a.dtype} min={a.min()} max={a.max()} "
      f"bits_used={int(a.max()).bit_length()}")

# Are the headers byte-identical between frames (i.e. is the overhead pure boilerplate)?
with open(TAR, "rb") as fh:
    heads = []
    for nm, sz, off in ms[:4]:
        fh.seek(off)
        heads.append(fh.read(200))
same = [sum(x == y for x, y in zip(heads[0], h)) for h in heads[1:]]
print(f"\nfirst-200-byte header agreement vs frame 0: {same} / 200")
