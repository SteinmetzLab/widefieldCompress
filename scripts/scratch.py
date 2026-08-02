"""Read-only: pull temporally-consecutive frames out of the widefield tar and characterise them.

Writes only to the local project dir. Nothing is written to Y: or Z:.
"""

import os
import tarfile

import numpy as np

TAR = r"Y:\Subjects\AL_0033\2025-03-05\1\widefield.tar"
H, W = 560, 560
FRAME_BYTES = H * W * 2
BLOCK = 512
STRIDE = 512 + ((FRAME_BYTES + 511) // 512) * 512  # header + padded data

total = os.path.getsize(TAR)
print(f"tar bytes        : {total:,d}  ({total/2**30:.1f} GiB)")
print(f"frame stride     : {STRIDE:,d}")
# layout: one 512 dir entry, then N frames, then 2 blocks of zeros (+ blocking factor pad)
n_est = (total - BLOCK) // STRIDE
print(f"estimated frames : {n_est:,d}   leftover={(total - BLOCK) - n_est*STRIDE:,d} bytes")

# The tar was built in lexicographic name order, so position != frame number.
names = sorted(f"1/frame-{i}" for i in range(n_est))
pos = {n: k for k, n in enumerate(names)}
print("first 8 in archive order:", [n.split('/')[1] for n in names[:8]])


def read_frame(fh, i):
    """Frame i (temporal index) as (H, W) uint16, seeking to its archive position."""
    k = pos[f"1/frame-{i}"]
    off = BLOCK + k * STRIDE
    fh.seek(off)
    hdr = fh.read(BLOCK)
    name = hdr[:100].rstrip(b"\0").decode()
    size = int(hdr[124:136].rstrip(b"\0 ").decode() or "0", 8)
    assert name == f"1/frame-{i}", f"expected 1/frame-{i}, header says {name!r}"
    assert size == FRAME_BYTES, size
    return np.frombuffer(fh.read(FRAME_BYTES), dtype="<u2").reshape(H, W)


N = 240  # temporally consecutive frames (blue/violet interleaved)
with open(TAR, "rb") as fh:
    # sanity-check the constant-stride assumption at both ends
    for probe in (0, 1, 2, n_est - 1):
        k = pos[f"1/frame-{probe}"]
        fh.seek(BLOCK + k * STRIDE)
        nm = fh.read(100).rstrip(b"\0").decode()
        print(f"  probe frame-{probe}: header name = {nm}")
    frames = np.stack([read_frame(fh, i) for i in range(N)])

print(f"\nframes {frames.shape} {frames.dtype}")
print(f"value range      : {frames.min()} .. {frames.max()}")
print(f"bits actually used: {int(frames.max()).bit_length()}")
print(f"low-bit histogram (is it left-shifted?): "
      f"{np.bincount(frames.ravel() & 0xF, minlength=16)[:4]} ...")
print(f"mean {frames.mean():.1f}  std {frames.std():.1f}")

# Blue/violet interleave: which parity is which?
idx = np.load(r"Y:\Subjects\AL_0033\2025-03-05\1\blueFrames.indexes.npy")
print(f"\nblueFrames.indexes: {idx.shape} {idx.dtype} first 10 = {idx[:10]}")
vio = np.load(r"Y:\Subjects\AL_0033\2025-03-05\1\violetFrames.indexes.npy")
print(f"violetFrames.indexes: {vio.shape} {vio.dtype} first 10 = {vio[:10]}")

np.save(r"D:\Dropbox\code\widefieldCompress\sample_frames.npy", frames)
print("\nsaved sample_frames.npy locally")
