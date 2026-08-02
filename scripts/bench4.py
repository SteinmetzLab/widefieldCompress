"""Two details that change the design: header redundancy, and full-height-strip JPEG-LS."""

import io
import sys
import time
import zlib

sys.path.insert(
    0,
    r"C:\Users\nicks\AppData\Local\Temp\claude\D--Dropbox-code-widefieldCompress"
    r"\b9d8ffb2-d8d7-4255-af3d-c32ab501922b\scratchpad\pylibs",
)
import imagecodecs
import numpy as np
import tifffile

BLOCK = 512
TAR = r"Y:\Subjects\ZYE_0095\2025-07-12\3\widefield.tar"  # basler-tiff flavour

# --- how redundant are the per-frame TIFF headers and the tar headers? --------------------------
tar_headers, tiff_headers = [], []
with open(TAR, "rb") as fh:
    off = 0
    while len(tiff_headers) < 200:
        fh.seek(off)
        h = fh.read(BLOCK)
        size = int(h[124:136].rstrip(b"\0 ").decode(errors="replace") or "0", 8)
        if size:
            tar_headers.append(h)
            tiff_headers.append(fh.read(4626))  # TIFF header + strip tables, before pixel data
        off += BLOCK + ((size + 511) // 512) * 512

th = b"".join(tar_headers)
fh_ = b"".join(tiff_headers)
print(f"tar headers : {len(th):>10,d} B -> zlib {len(zlib.compress(th, 9)):>8,d} B "
      f"(x{len(th)/len(zlib.compress(th,9)):.0f})")
print(f"TIFF headers: {len(fh_):>10,d} B -> zlib {len(zlib.compress(fh_, 9)):>8,d} B "
      f"(x{len(fh_)/len(zlib.compress(fh_,9)):.0f})")
diff = sum(a != b for a, b in zip(tiff_headers[0], tiff_headers[1]))
print(f"bytes differing between consecutive TIFF headers: {diff} / 4626")

n_frames_all = 272_000_000
print(f"\nTIFF-header bytes across the whole corpus: "
      f"{4626 * n_frames_all / 1e12:.2f} TB (0.73% of 170 TB)")

# --- JPEG-LS inside TIFF, one strip for the whole image -----------------------------------------
frames = np.load(r"D:\Dropbox\code\widefieldCompress\sample_frames.npy")[:64]
raw = frames.nbytes
print(f"\n{'variant':<46s}{'MiB':>8s}{'ratio':>7s}{'enc MB/s':>10s}")
print("-" * 71)


def try_tif(label, **kw):
    buf = io.BytesIO()
    t0 = time.perf_counter()
    with tifffile.TiffWriter(buf) as tw:
        for f in frames:
            tw.write(f, contiguous=False, **kw)
    te = time.perf_counter() - t0
    with tifffile.TiffFile(io.BytesIO(buf.getvalue())) as tf:
        back = np.stack([p.asarray() for p in tf.pages])
    assert np.array_equal(back, frames), f"{label} NOT LOSSLESS"
    n = buf.getbuffer().nbytes
    print(f"{label:<46s}{n/2**20:8.2f}{raw/n:7.2f}{raw/1e6/te:10.1f}")


try_tif("TIFF JPEG-LS, default strips", compression=34887)
try_tif("TIFF JPEG-LS, one strip (rowsperstrip=560)", compression=34887, rowsperstrip=560)
try_tif("TIFF deflate+pred2, one strip", compression="zlib", predictor=2, rowsperstrip=560)

# what a single-strip *uncompressed* TIFF costs in header (vs the 4626 B Basler writes)
buf = io.BytesIO()
tifffile.imwrite(buf, frames[0], rowsperstrip=560)
print(f"\nsingle-strip uncompressed TIFF header overhead: "
      f"{buf.getbuffer().nbytes - frames[0].nbytes} B (Basler writes 4,626 B)")
