"""Benchmark lossless compression strategies on real widefield frames. Local files only."""

import bz2
import lzma
import subprocess
import time
import zlib
from pathlib import Path

import numpy as np

HERE = Path(r"D:\Dropbox\code\widefieldCompress")
frames = np.load(HERE / "sample_frames.npy")  # (240, 560, 560) uint16, blue/violet interleaved
N, H, W = frames.shape
raw_size = frames.nbytes
print(f"input: {frames.shape} {frames.dtype}  {raw_size/2**20:.1f} MiB\n")

tmp = HERE / "_bench"
tmp.mkdir(exist_ok=True)


def zstd_size(buf: bytes, level: int) -> tuple[int, float]:
    p = tmp / "in.bin"
    p.write_bytes(buf)
    t0 = time.perf_counter()
    out = subprocess.run(
        ["zstd", f"-{level}", "-T0", "-f", "-q", str(p), "-o", str(tmp / "out.zst")],
        capture_output=True,
    )
    dt = time.perf_counter() - t0
    assert out.returncode == 0, out.stderr
    return (tmp / "out.zst").stat().st_size, dt


def report(label: str, nbytes: int, dt: float | None = None):
    ratio = raw_size / nbytes
    saved = 100 * (1 - nbytes / raw_size)
    speed = f"  {raw_size/2**20/dt:7.1f} MiB/s" if dt else ""
    print(f"  {label:38s} {nbytes/2**20:8.2f} MiB  x{ratio:5.2f}  {saved:5.1f}% saved{speed}")


# --- transforms -------------------------------------------------------------------------------
flat = frames.tobytes()

# byte-plane split: all low bytes, then all high bytes (high byte is 0..15 -> highly redundant)
shuffled = np.stack([frames & 0xFF, frames >> 8]).astype(np.uint8).tobytes()

# 12-bit packing: 2 samples -> 3 bytes (mechanical 25% win before any entropy coding)
def pack12(a: np.ndarray) -> bytes:
    v = a.ravel().astype(np.uint16)
    assert v.max() < 4096
    if v.size % 2:
        v = np.append(v, 0)
    a0, a1 = v[0::2].astype(np.uint32), v[1::2].astype(np.uint32)
    comb = a0 | (a1 << 12)
    return np.stack(
        [comb & 0xFF, (comb >> 8) & 0xFF, (comb >> 16) & 0xFF]
    ).astype(np.uint8).T.tobytes()


packed = pack12(frames)

# temporal delta within channel (blue = even, violet = odd -> step of 2)
tdelta = frames.astype(np.int32).copy()
tdelta[2:] -= frames[:-2].astype(np.int32)
tdelta_u = (tdelta.astype(np.int16)).view(np.uint16)
tdelta_shuf = np.stack([tdelta_u & 0xFF, tdelta_u >> 8]).astype(np.uint8).tobytes()

# naive temporal delta ignoring the interleave (step of 1) -- shows why interleave matters
tdelta1 = frames.astype(np.int32).copy()
tdelta1[1:] -= frames[:-1].astype(np.int32)
tdelta1_u = tdelta1.astype(np.int16).view(np.uint16)
tdelta1_shuf = np.stack([tdelta1_u & 0xFF, tdelta1_u >> 8]).astype(np.uint8).tobytes()

# horizontal spatial delta (what TIFF predictor=2 does)
hdelta = frames.astype(np.int32).copy()
hdelta[:, :, 1:] -= frames[:, :, :-1].astype(np.int32)
hdelta_u = hdelta.astype(np.int16).view(np.uint16)
hdelta_shuf = np.stack([hdelta_u & 0xFF, hdelta_u >> 8]).astype(np.uint8).tobytes()

print("=== zstd -3 (fast, the realistic bulk setting) ===")
for label, buf in [
    ("raw bytes", flat),
    ("byte-plane shuffle", shuffled),
    ("12-bit packed", packed),
    ("horiz delta + shuffle", hdelta_shuf),
    ("temporal delta step1 + shuffle", tdelta1_shuf),
    ("temporal delta step2 + shuffle", tdelta_shuf),
]:
    n, dt = zstd_size(buf, 3)
    report(label, n, dt)

print("\n=== zstd -9 ===")
for label, buf in [
    ("raw bytes", flat),
    ("byte-plane shuffle", shuffled),
    ("horiz delta + shuffle", hdelta_shuf),
    ("temporal delta step2 + shuffle", tdelta_shuf),
]:
    n, dt = zstd_size(buf, 9)
    report(label, n, dt)

print("\n=== zstd -19 (slow, archival) ===")
for label, buf in [("byte-plane shuffle", shuffled), ("temporal delta step2 + shuffle", tdelta_shuf)]:
    n, dt = zstd_size(buf, 19)
    report(label, n, dt)

print("\n=== stdlib reference ===")
t0 = time.perf_counter(); n = len(zlib.compress(shuffled, 6)); report("zlib-6 byte-shuffle", n, time.perf_counter()-t0)
t0 = time.perf_counter(); n = len(lzma.compress(shuffled, preset=1)); report("lzma-1 byte-shuffle", n, time.perf_counter()-t0)
t0 = time.perf_counter(); n = len(bz2.compress(tdelta_shuf, 9)); report("bz2-9 tdelta2", n, time.perf_counter()-t0)

print("\n=== theoretical floor: order-0 entropy ===")
for label, arr in [
    ("raw uint16 symbols", frames.ravel()),
    ("temporal delta step2 symbols", tdelta[2:].ravel()),
]:
    vals, cnt = np.unique(arr, return_counts=True)
    p = cnt / cnt.sum()
    ent = -(p * np.log2(p)).sum()
    print(f"  {label:38s} {ent:5.2f} bits/sample -> x{16/ent:5.2f}")
