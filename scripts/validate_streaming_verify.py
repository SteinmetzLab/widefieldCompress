"""Re-run pilot sessions with streaming verification: same verdict, less I/O?

Compares against the decompress-and-diff route on the same file, so the cheap check is validated
against the expensive one on real data rather than only on synthetic tests.
"""

from __future__ import annotations

import time
from pathlib import Path

from wfcompress import codec, sha256_file, verify
from wfcompress.lab.session import session_frame_shape

OUT = Path(r"Y:\temp\wfCompressTest")
SESSIONS = [
    Path(r"Y:\Subjects\AL_0048\2026-06-11\4\widefield.tar"),   # frame-N, 3.3 GB
    Path(r"Y:\Subjects\ZYE_0008\2020-07-25\1\widefield.tar"),  # basler, 20.3 GB
]

for src in SESSIONS:
    n = src.stat().st_size
    dst = OUT / f"validate_{src.parent.parts[-3]}_{src.parent.parts[-2]}.wfz"
    print(f"\n=== {src}  ({n/1e9:.2f} GB)")

    t0 = time.perf_counter()
    meta = codec.compress(src, dst, shape=session_frame_shape(src))
    t_comp = time.perf_counter() - t0
    print(f"  compress          x{meta['ratio']:.2f}  {t_comp/60:5.1f} min  "
          f"{n/1e6/t_comp:5.1f} MB/s")

    t0 = time.perf_counter()
    v = verify(dst)
    t_verify = time.perf_counter() - t0
    print(f"  stream verify     {t_verify/60:5.1f} min  byte_identical={v['byte_identical']}")

    # the expensive route, for comparison and as a cross-check of the cheap one
    restored = dst.with_suffix(".restored.tar")
    t0 = time.perf_counter()
    codec.decompress(dst, restored)
    h_restored = sha256_file(restored)
    h_src = sha256_file(src)
    t_full = time.perf_counter() - t0
    restored.unlink()
    print(f"  decompress+diff   {t_full/60:5.1f} min  identical={h_src == h_restored}")

    agree = (v["tar_sha256"] == h_restored == h_src)
    print(f"  the two routes {'AGREE' if agree else '*** DISAGREE ***'}")
    io_stream = n + meta["output_bytes"] + meta["output_bytes"]
    io_full = n + meta["output_bytes"] + meta["output_bytes"] + n + 2 * n
    print(f"  I/O  stream {io_stream/n:.1f}x vs full {io_full/n:.1f}x source bytes   "
          f"total time {(t_comp+t_verify)/60:.1f} vs {(t_comp+t_full)/60:.1f} min")
    dst.unlink()
    for extra in (dst.with_name(dst.name + ".README.md"), dst.with_name(dst.name + ".receipt.json")):
        if extra.exists():
            extra.unlink()
