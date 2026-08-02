"""Codec shootout on real widefield frames. Local files only; measures ratio AND throughput."""

import sys
import time
from pathlib import Path

sys.path.insert(
    0,
    r"C:\Users\nicks\AppData\Local\Temp\claude\D--Dropbox-code-widefieldCompress"
    r"\b9d8ffb2-d8d7-4255-af3d-c32ab501922b\scratchpad\pylibs",
)

import imagecodecs
import numpy as np
import zstandard as zstd

frames = np.load(r"D:\Dropbox\code\widefieldCompress\sample_frames.npy")  # (240,560,560) u16
N = 64  # keep the slow codecs tractable
sub = frames[:N]
raw = sub.nbytes
print(f"{sub.shape} {sub.dtype}  {raw/2**20:.1f} MiB, 12-bit data in 16-bit containers\n")
print(f"{'codec':<44s}{'MiB':>8s}{'ratio':>8s}{'saved':>8s}{'enc MB/s':>10s}{'dec MB/s':>10s}")
print("-" * 88)


def run(label, enc, dec=None, data=sub):
    t0 = time.perf_counter()
    out = enc(data)
    te = time.perf_counter() - t0
    n = len(out) if isinstance(out, (bytes, bytearray)) else sum(len(b) for b in out)
    td = None
    if dec is not None:
        t0 = time.perf_counter()
        back = dec(out)
        td = time.perf_counter() - t0
        assert np.array_equal(np.asarray(back).reshape(data.shape), data), f"{label} NOT LOSSLESS"
    print(
        f"{label:<44s}{n/2**20:8.2f}{raw/n:8.2f}{100*(1-n/raw):7.1f}%"
        f"{raw/1e6/te:10.1f}{(raw/1e6/td) if td else float('nan'):10.1f}"
    )
    return n


# --- whole-block generic compressors -----------------------------------------------------------
def zst(level, threads=0):
    c = zstd.ZstdCompressor(level=level, threads=threads)
    d = zstd.ZstdDecompressor()
    return (lambda a: c.compress(a.tobytes()),
            lambda b: np.frombuffer(d.decompress(b, max_output_size=raw), dtype=np.uint16))


e, d = zst(3);  run("zstd-3 raw", e, d)
e, d = zst(9);  run("zstd-9 raw", e, d)

shuf = lambda a: np.stack([a & 0xFF, a >> 8]).astype(np.uint8)
unshuf = lambda b: (b[1].astype(np.uint16) << 8) | b[0]
e, _ = zst(3); run("zstd-3 byte-shuffle", lambda a: e(shuf(a)))
e, _ = zst(9); run("zstd-9 byte-shuffle", lambda a: e(shuf(a)))

# --- per-frame image codecs (the realistic unit of work) ----------------------------------------
def per_frame(encode, decode, label, arr=sub):
    run(label,
        lambda a: [encode(f) for f in a],
        lambda bs: np.stack([decode(b) for b in bs]),
        arr)


per_frame(lambda f: imagecodecs.png_encode(f, level=1), imagecodecs.png_decode, "PNG level-1")
per_frame(lambda f: imagecodecs.png_encode(f, level=6), imagecodecs.png_decode, "PNG level-6")
per_frame(imagecodecs.jpegls_encode, imagecodecs.jpegls_decode, "JPEG-LS (lossless)")
per_frame(lambda f: imagecodecs.jpeg2k_encode(f, level=0, reversible=True),
          imagecodecs.jpeg2k_decode, "JPEG-2000 reversible")
for eff in (3, 7):
    per_frame(lambda f, e=eff: imagecodecs.jpegxl_encode(f, lossless=True, effort=e),
              imagecodecs.jpegxl_decode, f"JPEG-XL lossless effort={eff}")

# TIFF-native options (stay a valid TIFF; universally readable)
for name, comp in [("deflate", "zlib"), ("lzw", "lzw"), ("zstd", "zstd")]:
    try:
        per_frame(
            lambda f, c=comp: imagecodecs.tiff_encode(f, compression=c, predictor=True),
            imagecodecs.tiff_decode,
            f"TIFF {name} + predictor-2",
        )
    except Exception as ex:  # noqa: BLE001
        print(f"TIFF {name}: {type(ex).__name__}: {str(ex)[:60]}")

# --- exploit the blue/violet temporal structure -------------------------------------------------
# same-channel delta (step 2), offset into uint16 so image codecs can take it
td = sub.astype(np.int32).copy()
td[2:] -= sub[:-2].astype(np.int32)
td_u = (td + 32768).clip(0, 65535).astype(np.uint16)
assert np.array_equal((td_u.astype(np.int32) - 32768), td)

e, _ = zst(3); run("zstd-3 shuffle, same-chan delta", lambda a: e(shuf(a)), data=td_u)
e, _ = zst(9); run("zstd-9 shuffle, same-chan delta", lambda a: e(shuf(a)), data=td_u)
per_frame(imagecodecs.jpegls_encode, imagecodecs.jpegls_decode,
          "JPEG-LS on same-chan delta", td_u)
per_frame(lambda f: imagecodecs.jpegxl_encode(f, lossless=True, effort=3),
          imagecodecs.jpegxl_decode, "JPEG-XL e3 on same-chan delta", td_u)

# --- de-interleave into two single-channel movies, then codec ------------------------------------
blue, violet = sub[0::2], sub[1::2]
print("\n(de-interleaved: blue and violet as separate stacks)")
for nm, st in (("blue", blue), ("violet", violet)):
    e, _ = zst(3)
    run(f"zstd-3 shuffle {nm}-only", lambda a: e(shuf(a)), data=st)
