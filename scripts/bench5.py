"""Does mean-subtraction help? And is the ratio the same for basler-tiff vs frame-N data?

Also: where is the information-theoretic floor, given this is shot-noise-limited imaging?
"""

import io
import sys
import time

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
    """First n members' pixel arrays, walking headers sequentially."""
    out = []
    off = 0
    with open(tar, "rb") as fh:
        while len(out) < n:
            fh.seek(off)
            h = fh.read(BLOCK)
            if not h or h[:1] == b"\0":
                break
            size = int(h[124:136].rstrip(b"\0 ").decode(errors="replace") or "0", 8)
            if size:
                raw = fh.read(size)
                if decode_tiff:
                    out.append(tifffile.imread(io.BytesIO(raw)))
                else:
                    side = int((size // 2) ** 0.5)
                    out.append(np.frombuffer(raw, "<u2").reshape(side, side))
            off += BLOCK + ((size + 511) // 512) * 512
    return np.stack(out)


# ------------------------------------------------------------------ codec harness
def jls(a):
    return sum(len(imagecodecs.jpegls_encode(f)) for f in a)


def zstd_shuf(a, level=3):
    c = zstd.ZstdCompressor(level=level, threads=0)
    return len(c.compress(np.stack([a & 0xFF, a >> 8]).astype(np.uint8).tobytes()))


def bits_per_sample(nbytes, arr):
    return 8 * nbytes / arr.size


def show(label, nbytes, arr, base=None):
    bps = bits_per_sample(nbytes, arr)
    ratio = arr.nbytes / nbytes
    rel = f"   ({100*(nbytes/base - 1):+.1f}% vs raw JPEG-LS)" if base else ""
    print(f"  {label:<44s} x{ratio:5.2f}   {bps:5.2f} bits/px{rel}")
    return nbytes


def to_u16(signed):
    """Shift a signed residual into uint16 using the smallest offset that works."""
    lo = int(signed.min())
    out = (signed - lo).astype(np.uint16)
    assert np.array_equal(out.astype(np.int32) + lo, signed)
    return out


def analyse(name, frames):
    print(f"\n{'='*78}\n{name}: {frames.shape} {frames.dtype}  "
          f"range {frames.min()}..{frames.max()}  ({frames.nbytes/2**20:.1f} MiB)")
    blue, violet = frames[0::2], frames[1::2]
    print(f"  blue mean {blue.mean():7.1f}   violet mean {violet.mean():7.1f}")

    # --- the noise floor -------------------------------------------------------------------
    # per-pixel temporal std within each channel = the irreducible shot-noise content
    sd = np.concatenate([blue.std(axis=0).ravel(), violet.std(axis=0).ravel()])
    sd_med = float(np.median(sd))
    # differential entropy of a Gaussian discretised to unit (1 ADU) bins
    floor = float(np.mean(np.log2(np.sqrt(2 * np.pi * np.e) * np.maximum(sd, 0.5))))
    print(f"  per-pixel temporal std: median {sd_med:.1f} ADU")
    print(f"  Gaussian noise floor  : {floor:.2f} bits/px  -> best possible x{16/floor:.2f}")

    print("\n  --- as stored ---")
    base = show("JPEG-LS, raw frames", jls(frames), frames)
    show("zstd-3 + byte shuffle", zstd_shuf(frames), frames)

    print("\n  --- your idea: subtract the mean image ---")
    # per-channel mean image, rounded to integer so the transform stays exactly invertible
    mb = np.round(blue.mean(axis=0)).astype(np.int32)
    mv = np.round(violet.mean(axis=0)).astype(np.int32)
    resid = frames.astype(np.int32).copy()
    resid[0::2] -= mb
    resid[1::2] -= mv
    r_u = to_u16(resid)
    print(f"  residual range {resid.min()}..{resid.max()}  "
          f"(raw spans {frames.max()-frames.min()})")
    show("JPEG-LS, mean-subtracted", jls(r_u), r_u, base)
    show("zstd-3 + shuffle, mean-subtracted", zstd_shuf(r_u), r_u)

    print("\n  --- controls ---")
    # a pure scalar DC shift: proves whether the codec is shift-invariant
    dc = to_u16(frames.astype(np.int32) - int(frames.mean()))
    show("JPEG-LS, scalar mean removed", jls(dc), dc, base)
    # same-channel temporal delta, for comparison
    td = frames.astype(np.int32).copy()
    td[2:] -= frames[:-2].astype(np.int32)
    show("JPEG-LS, same-channel temporal delta", jls(to_u16(td)), frames, base)
    # mean-subtracted AND temporally delta'd
    show("JPEG-LS, mean-sub then temporal delta",
         jls(to_u16(np.concatenate([resid[:2], resid[2:] - resid[:-2]]))), frames, base)


analyse(
    "frame-N flavour  (AL_0033 2025-03-05/1)",
    read_tar_frames(r"Y:\Subjects\AL_0033\2025-03-05\1\widefield.tar", 0, False)
    if False
    else np.load(r"D:\Dropbox\code\widefieldCompress\sample_frames.npy")[:120],
)

analyse(
    "basler-tiff flavour  (ZYE_0095 2025-07-12/3)",
    read_tar_frames(r"Y:\Subjects\ZYE_0095\2025-07-12\3\widefield.tar", 120, True),
)
