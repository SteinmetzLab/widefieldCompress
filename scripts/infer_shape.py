"""Recover frame geometry for headerless archives by asking which shape compresses best.

A wrong shape puts unrelated pixels next to each other, so JPEG-LS's spatial predictor fails and
the frame encodes much larger. The correct shape should win by a wide, unambiguous margin.

Validated first against archives whose geometry is known independently, then applied to the ones
that have no meanImage.npy.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import imagecodecs
import numpy as np

from wfcompress.lab.session import session_frame_shape

HERE = Path(__file__).resolve().parents[1]
BLOCK = 512


def candidate_shapes(n_pixels: int, min_aspect: float = 0.25) -> list[tuple[int, int]]:
    """All (rows, cols) factorisations with a plausible aspect ratio."""
    out = []
    for r in range(1, int(n_pixels**0.5) + 1):
        if n_pixels % r:
            continue
        c = n_pixels // r
        for rows, cols in ((r, c), (c, r)):
            if min_aspect <= rows / cols <= 1 / min_aspect:
                out.append((rows, cols))
    return sorted(set(out))


def read_frames(tar: Path, n: int = 4) -> tuple[list[bytes], int]:
    """First n data members as raw bytes."""
    out, off = [], 0
    with open(tar, "rb") as fh:
        while len(out) < n:
            fh.seek(off)
            h = fh.read(BLOCK)
            if len(h) < BLOCK or h[:1] == b"\0":
                break
            size = int(h[124:136].rstrip(b"\0 ").decode("ascii", "replace") or "0", 8)
            if size:
                out.append(fh.read(size))
            off += BLOCK + ((size + 511) // 512) * 512
    return out, (len(out[0]) if out else 0)


def score_shapes(raws: list[bytes], shapes: list[tuple[int, int]]) -> list[tuple]:
    """Compressed size per shape, summed over the sample frames. Smaller is better."""
    results = []
    for rows, cols in shapes:
        total = 0
        for raw in raws:
            a = np.frombuffer(raw, "<u2").reshape(rows, cols)
            om = int(np.bitwise_or.reduce(a.ravel()))
            shift = ((om & -om).bit_length() - 1) if om else 0
            total += len(imagecodecs.jpegls_encode((a >> shift).astype(np.uint16)))
        results.append((total, (rows, cols)))
    return sorted(results)


def infer(tar: Path, n_frames: int = 4, verbose: bool = False):
    raws, member_bytes = read_frames(tar, n_frames)
    if not raws:
        return None
    if raws[0][:2] in (b"II", b"MM"):
        return "TIFF"  # carries its own geometry, nothing to infer
    shapes = candidate_shapes(member_bytes // 2)
    scored = score_shapes(raws, shapes)
    best, runner = scored[0], scored[1] if len(scored) > 1 else None
    margin = (runner[0] / best[0]) if runner else float("inf")
    if verbose:
        for size, shp in scored[:5]:
            print(f"      {str(shp):>14s}  {size/1e6:7.3f} MB  {size/best[0]:5.2f}x")
    return {"shape": best[1], "margin": margin, "n_candidates": len(shapes),
            "member_bytes": member_bytes}


if __name__ == "__main__":
    cov = list(csv.DictReader((HERE / "data" / "geometry_coverage.csv").open(encoding="utf-8")))
    headerless = [r for r in cov if r["headerless"] == "True"]
    known = [r for r in headerless if r["blocked"] != "True"][:8]
    blocked = [r for r in headerless if r["blocked"] == "True"]

    print("=== validation: archives whose geometry we already know ===")
    hits = 0
    for r in known:
        truth = session_frame_shape(Path(r["path"]))
        got = infer(Path(r["path"]))
        ok = got and got["shape"] == truth
        hits += bool(ok)
        print(f"  {'OK ' if ok else 'MISS'} truth={truth}  inferred={got['shape']}  "
              f"margin={got['margin']:.2f}x over {got['n_candidates']} candidates  "
              f"{Path(r['path']).parent}")
    print(f"\n  {hits}/{len(known)} correct")

    if hits < len(known):
        print("\n  method is not reliable enough to use; stopping")
        sys.exit(1)

    print("\n=== the blocked archives ===")
    rows_out = []
    for r in sorted(blocked, key=lambda r: -int(r["bytes"])):
        got = infer(Path(r["path"]))
        print(f"  {int(r['bytes'])/1e9:8.1f} GB  inferred {str(got['shape']):>12s}  "
              f"margin {got['margin']:5.2f}x  ({got['n_candidates']} candidates)  "
              f"{Path(r['path']).parent}")
        rows_out.append({"path": r["path"], "bytes": r["bytes"],
                         "rows": got["shape"][0], "cols": got["shape"][1],
                         "margin": round(got["margin"], 3)})

    out = HERE / "data" / "inferred_shapes.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["path", "bytes", "rows", "cols", "margin"])
        w.writeheader()
        w.writerows(rows_out)
    print(f"\nwrote {out}")
