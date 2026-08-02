"""Final word on mean subtraction, measured on bit-shift-normalised data."""

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

BLOCK = 512


def read_tar_frames(tar, n, tiff=None):
    """Sniff each member: TIFF members carry their own geometry, raw ones take it from
    the session's meanImage.npy (frame-N members are not necessarily square)."""
    shape = None
    mi = __import__("pathlib").Path(tar).parent / "blue" / "meanImage.npy"
    if mi.is_file():
        shape = np.load(mi, mmap_mode="r").shape
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
                if raw[:2] in (b"II", b"MM"):
                    out.append(np.asarray(tifffile.imread(io.BytesIO(raw))))
                else:
                    sh = shape if shape and shape[0] * shape[1] * 2 == size else \
                        2 * (int((size // 2) ** 0.5),)
                    out.append(np.frombuffer(raw, "<u2").reshape(sh))
            off += BLOCK + ((size + 511) // 512) * 512
    return np.stack(out)


def jls(a):
    return sum(len(imagecodecs.jpegls_encode(x)) for x in a)


def to_u16(s):
    lo = int(s.min())
    o = (s - lo).astype(np.uint16)
    assert np.array_equal(o.astype(np.int32) + lo, s)
    return o


def normalise(f):
    """Strip always-zero low bits. Returns (data, shift) — exactly invertible."""
    om = int(np.bitwise_or.reduce(f.ravel()))
    sh = ((om & -om).bit_length() - 1) if om else 0
    g = (f >> sh).astype(np.uint16)
    assert np.array_equal(g.astype(np.uint32) << sh, f)
    return g, sh


SESSIONS = {
    "AL_0033 2025-03-05 (frame-N)": np.load(
        r"D:\Dropbox\code\widefieldCompress\sample_frames.npy")[:120],
    "ZYE_0095 2025-07-12 (basler)": read_tar_frames(
        r"Y:\Subjects\ZYE_0095\2025-07-12\3\widefield.tar", 120),
    "AL_0048 2026-07-01 (basler)": read_tar_frames(
        r"Y:\Subjects\AL_0048\2026-07-01\6\widefield.tar", 120),
}

for name, f in SESSIONS.items():
    g, sh = normalise(f)
    orig = f.nbytes
    print(f"\n{'='*74}\n{name}   shift={sh}  payload={int(np.bitwise_or.reduce(g.ravel())).bit_length()} bits")

    blue, violet = g[0::2].astype(float), g[1::2].astype(float)
    sd = np.concatenate([blue.std(axis=0).ravel(), violet.std(axis=0).ravel()])
    floor = float(np.mean(np.log2(np.sqrt(2 * np.pi * np.e) * np.maximum(sd, 0.5))))
    print(f"  per-pixel temporal std {np.median(sd):.1f}   noise floor {floor:.2f} bits "
          f"-> ceiling x{16/floor:.2f}")

    base = jls(g)
    print(f"  {'JPEG-LS, bit-shift normalised':<42s} x{orig/base:5.2f}"
          f"   {8*base/g.size:5.2f} bits/px    <- baseline")

    mb = np.round(g[0::2].mean(axis=0)).astype(np.int32)
    mv = np.round(g[1::2].mean(axis=0)).astype(np.int32)
    r = g.astype(np.int32).copy()
    r[0::2] -= mb
    r[1::2] -= mv
    n = jls(to_u16(r))
    print(f"  {'  + per-channel mean image subtracted':<42s} x{orig/n:5.2f}"
          f"   {8*n/g.size:5.2f} bits/px   {100*(n/base-1):+5.1f}%")

    # does a drifting (block-local) mean do better than one global mean?
    r2 = g.astype(np.int32).copy()
    blk = 40
    for s in range(0, len(g), blk):
        c = g[s:s + blk]
        r2[s:s + blk:2] -= np.round(c[0::2].mean(axis=0)).astype(np.int32)
        r2[s + 1:s + blk:2] -= np.round(c[1::2].mean(axis=0)).astype(np.int32)
    n2 = jls(to_u16(r2))
    print(f"  {'  + block-local mean (40 frames)':<42s} x{orig/n2:5.2f}"
          f"   {8*n2/g.size:5.2f} bits/px   {100*(n2/base-1):+5.1f}%")

    print(f"  {'(for reference: JPEG-LS without the shift)':<42s} x{orig/jls(f):5.2f}")
