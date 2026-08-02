"""Container question: what can still be read by ordinary tools, and at what cost?"""

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

frames = np.load(r"D:\Dropbox\code\widefieldCompress\sample_frames.npy")[:64]
raw = frames.nbytes
print(f"{frames.shape}  {raw/2**20:.1f} MiB\n")
print(f"{'container / codec':<44s}{'MiB':>8s}{'ratio':>7s}{'enc MB/s':>10s}{'dec MB/s':>10s}")
print("-" * 79)


def rep(label, nbytes, te, td):
    print(f"{label:<44s}{nbytes/2**20:8.2f}{raw/nbytes:7.2f}"
          f"{raw/1e6/te:10.1f}{raw/1e6/td:10.1f}")


def tif_stack(label, comp, predictor=None):
    """One multi-page TIFF holding all frames, written page by page."""
    buf = io.BytesIO()
    t0 = time.perf_counter()
    with tifffile.TiffWriter(buf) as tw:
        for f in frames:
            tw.write(f, compression=comp, predictor=predictor, contiguous=False)
    te = time.perf_counter() - t0
    n = buf.getbuffer().nbytes

    t0 = time.perf_counter()
    with tifffile.TiffFile(io.BytesIO(buf.getvalue())) as tf:
        back = np.stack([p.asarray() for p in tf.pages])
    td = time.perf_counter() - t0
    assert back.shape == frames.shape and np.array_equal(back, frames), f"{label} NOT LOSSLESS"
    rep(label, n, te, td)


for label, comp, pred in [
    ("multipage TIFF, deflate", "zlib", None),
    ("multipage TIFF, deflate + predictor-2", "zlib", 2),
    ("multipage TIFF, lzw + predictor-2", "lzw", 2),
    ("multipage TIFF, zstd-3 + predictor-2", "zstd", 2),
    ("multipage TIFF, JPEG-XL lossless", "jpegxl", None),
]:
    try:
        tif_stack(label, comp, pred)
    except Exception as ex:  # noqa: BLE001
        print(f"{label:<44s}  FAILED: {type(ex).__name__}: {str(ex)[:44]}")

# JPEG-LS inside TIFF: tifffile has no 'jpegls' alias, but the TIFF tag exists (34887).
try:
    tif_stack("multipage TIFF, JPEG-LS (tag 34887)", 34887)
except Exception as ex:  # noqa: BLE001
    print(f"{'multipage TIFF, JPEG-LS (tag 34887)':<44s}  FAILED: {str(ex)[:44]}")

# Bare JPEG-LS codestreams (no TIFF wrapper) -- one per frame, kept inside the tar.
t0 = time.perf_counter()
enc = [imagecodecs.jpegls_encode(f) for f in frames]
te = time.perf_counter() - t0
t0 = time.perf_counter()
back = np.stack([imagecodecs.jpegls_decode(b) for b in enc])
td = time.perf_counter() - t0
assert np.array_equal(back, frames)
rep("bare JPEG-LS codestream per frame", sum(map(len, enc)), te, td)
