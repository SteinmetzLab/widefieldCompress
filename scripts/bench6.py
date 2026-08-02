"""zstd beat JPEG-LS on ZYE_0095 *and* beat my noise floor. Something structural is going on."""

import io
import sys

sys.path.insert(
    0,
    r"C:\Users\nicks\AppData\Local\Temp\claude\D--Dropbox-code-widefieldCompress"
    r"\b9d8ffb2-d8d7-4255-af3d-c32ab501922b\scratchpad\pylibs",
)
import imagecodecs
import numpy as np
import tifffile
import zstandard as zstd

BLOCK = 512


def read_tar_frames(tar, n, decode_tiff):
    out, off = [], 0
    with open(tar, "rb") as fh:
        while len(out) < n:
            fh.seek(off)
            h = fh.read(BLOCK)
            if not h or h[:1] == b"\0":
                break
            size = int(h[124:136].rstrip(b"\0 ").decode(errors="replace") or "0", 8)
            if size:
                raw = fh.read(size)
                out.append(
                    tifffile.imread(io.BytesIO(raw))
                    if decode_tiff
                    else np.frombuffer(raw, "<u2").reshape(*(2 * [int((size // 2) ** 0.5)]))
                )
            off += BLOCK + ((size + 511) // 512) * 512
    return np.stack(out)


SESSIONS = {
    "AL_0033 2025-03-05 (frame-N)": np.load(
        r"D:\Dropbox\code\widefieldCompress\sample_frames.npy"
    )[:120],
    "ZYE_0095 2025-07-12 (basler)": read_tar_frames(
        r"Y:\Subjects\ZYE_0095\2025-07-12\3\widefield.tar", 120, True
    ),
}

for name, f in SESSIONS.items():
    print(f"\n{'='*76}\n{name}   range {f.min()}..{f.max()}")
    v = f.ravel()
    # which bit positions are ever set?
    setmask = int(np.bitwise_or.reduce(v))
    zeromask = int(np.bitwise_and.reduce(v))
    print(f"  OR  of all samples: {setmask:016b}")
    print(f"  AND of all samples: {zeromask:016b}")
    low_always_zero = (setmask & -setmask).bit_length() - 1 if setmask else 0
    print(f"  low bits always zero : {low_always_zero}")
    print(f"  high bits always zero: {16 - setmask.bit_length()}")
    print(f"  distinct values used : {len(np.unique(v)):,d}")
    gaps = np.diff(np.unique(v))
    print(f"  spacing between used values: min {gaps.min()} median {np.median(gaps):.0f} "
          f"max {gaps.max()}")
    print(f"  effective payload    : "
          f"{setmask.bit_length() - low_always_zero} bits/sample")


def jls(a):
    return sum(len(imagecodecs.jpegls_encode(x)) for x in a)


def zs(a, lvl=3):
    c = zstd.ZstdCompressor(level=lvl, threads=0)
    return len(c.compress(np.stack([a & 0xFF, a >> 8]).astype(np.uint8).tobytes()))


print(f"\n\n{'='*76}\nEffect of stripping always-zero low bits before encoding")
print(f"{'session / variant':<46s}{'ratio':>8s}{'bits/px':>10s}")
print("-" * 66)
for name, f in SESSIONS.items():
    v = f.ravel()
    setmask = int(np.bitwise_or.reduce(v))
    shift = (setmask & -setmask).bit_length() - 1 if setmask else 0
    print(f"{name}  (shift={shift})")
    n = jls(f)
    print(f"  {'JPEG-LS as stored':<44s}{f.nbytes/n:8.2f}{8*n/f.size:10.2f}")
    n = zs(f)
    print(f"  {'zstd-3 + shuffle as stored':<44s}{f.nbytes/n:8.2f}{8*n/f.size:10.2f}")
    if shift:
        g = (f >> shift).astype(np.uint16)
        assert np.array_equal((g.astype(np.uint32) << shift), f), "shift not invertible"
        n = jls(g)
        print(f"  {'JPEG-LS after >> ' + str(shift):<44s}{f.nbytes/n:8.2f}{8*n/f.size:10.2f}")
        n = zs(g)
        print(f"  {'zstd-3 + shuffle after >> ' + str(shift):<44s}"
              f"{f.nbytes/n:8.2f}{8*n/f.size:10.2f}")
        # noise floor recomputed on the de-shifted data
        b, vi = g[0::2].astype(float), g[1::2].astype(float)
        sd = np.concatenate([b.std(axis=0).ravel(), vi.std(axis=0).ravel()])
        floor = float(np.mean(np.log2(np.sqrt(2 * np.pi * np.e) * np.maximum(sd, 0.5))))
        print(f"  {'-> noise floor on de-shifted data':<44s}{16/floor:8.2f}{floor:10.2f}")
