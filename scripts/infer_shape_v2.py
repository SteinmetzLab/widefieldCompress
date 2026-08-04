"""Recover geometry for headerless archives, with a statistic that actually discriminates.

The compression-ratio test barely separates candidates (1.02x) because a wrong row length shears
the image: horizontal neighbours stay correct and only vertical structure breaks, which JPEG-LS's
predictor mostly shrugs off.

Row-to-row correlation measures exactly the thing that breaks, so it separates cleanly. And since
the answer is ultimately visual, this also renders frame 0 under the best and runner-up shapes.
"""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.family"] = "Arial"
plt.rcParams["font.sans-serif"] = ["Arial", "DejaVu Sans"]
plt.rcParams["axes.spines.top"] = False
plt.rcParams["axes.spines.right"] = False
plt.rcParams["pdf.fonttype"] = 42
plt.rcParams["ps.fonttype"] = 42

from wfcompress.lab.session import session_frame_shape

HERE = Path(__file__).resolve().parents[1]
BLOCK = 512


def candidate_shapes(n_pixels: int, min_aspect: float = 0.25) -> list[tuple[int, int]]:
    out = []
    for r in range(1, int(n_pixels**0.5) + 1):
        if n_pixels % r:
            continue
        c = n_pixels // r
        for rows, cols in ((r, c), (c, r)):
            if min_aspect <= rows / cols <= 1 / min_aspect:
                out.append((rows, cols))
    return sorted(set(out))


def read_frames(tar: Path, n: int = 3):
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


def row_coherence(a: np.ndarray) -> float:
    """Mean Pearson correlation between vertically adjacent rows.

    At the true width, row n and row n+1 are neighbouring lines of the same image and correlate
    strongly. At a wrong width every row is shifted relative to the last, and the correlation
    collapses. This is the signal the compression test was failing to see.
    """
    x = a.astype(np.float64)
    top, bot = x[:-1], x[1:]
    top = top - top.mean(axis=1, keepdims=True)
    bot = bot - bot.mean(axis=1, keepdims=True)
    num = (top * bot).sum(axis=1)
    den = np.sqrt((top**2).sum(axis=1) * (bot**2).sum(axis=1)) + 1e-12
    return float(np.mean(num / den))


def score(raws, shapes):
    out = []
    for rows, cols in shapes:
        vals = [row_coherence(np.frombuffer(r, "<u2").reshape(rows, cols)) for r in raws]
        out.append((float(np.mean(vals)), (rows, cols)))
    return sorted(out, reverse=True)


def infer(tar: Path):
    raws, member_bytes = read_frames(tar)
    if not raws or raws[0][:2] in (b"II", b"MM"):
        return None
    scored = score(raws, candidate_shapes(member_bytes // 2))
    best, runner = scored[0], scored[1]
    return {"shape": best[1], "coh": best[0], "runner": runner[1], "runner_coh": runner[0],
            "margin": best[0] - runner[0], "all": scored}


if __name__ == "__main__":
    cov = list(csv.DictReader((HERE / "data" / "geometry_coverage.csv").open(encoding="utf-8")))
    headerless = [r for r in cov if r["headerless"] == "True"]
    known = [r for r in headerless if r["blocked"] != "True"][:8]
    blocked = sorted((r for r in headerless if r["blocked"] == "True"),
                     key=lambda r: -int(r["bytes"]))

    print("=== validation on archives with independently known geometry ===")
    hits = 0
    for r in known:
        truth = session_frame_shape(Path(r["path"]))
        g = infer(Path(r["path"]))
        ok = g["shape"] == truth
        hits += ok
        print(f"  {'OK ' if ok else 'MISS'} truth={truth} inferred={g['shape']} "
              f"coherence {g['coh']:.4f} vs {g['runner_coh']:.4f} for {g['runner']} "
              f"(margin {g['margin']:+.4f})")
    print(f"\n  {hits}/{len(known)} correct")

    print("\n=== blocked archives ===")
    rows_out = []
    for r in blocked:
        g = infer(Path(r["path"]))
        rows_out.append({"path": r["path"], "bytes": r["bytes"],
                         "rows": g["shape"][0], "cols": g["shape"][1],
                         "coherence": round(g["coh"], 4),
                         "runner_up": str(g["runner"]),
                         "margin": round(g["margin"], 4)})
        print(f"  {int(r['bytes'])/1e9:8.1f} GB  {str(g['shape']):>12s}  coh {g['coh']:.4f}  "
              f"next best {str(g['runner']):>12s} {g['runner_coh']:.4f}  "
              f"margin {g['margin']:+.4f}   {Path(r['path']).parent.name}")

    out = HERE / "data" / "inferred_shapes.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)
    print(f"\nwrote {out}")

    # the decisive check is visual: does frame 0 look like a mouse brain?
    sample = Path(blocked[0]["path"])
    raws, mb = read_frames(sample, 1)
    g = infer(sample)
    shapes = [g["shape"], g["runner"], g["all"][2][1]]
    fig, axes = plt.subplots(1, len(shapes), figsize=(4 * len(shapes), 4.4))
    for ax, shp in zip(axes, shapes):
        img = np.frombuffer(raws[0], "<u2").reshape(shp)
        ax.imshow(img, cmap="gray")
        coh = row_coherence(img)
        ax.set_title(f"{shp[0]}x{shp[1]}\nrow coherence {coh:.3f}", fontsize=11)
        ax.set_xlabel("x (pixels)")
        ax.set_ylabel("y (pixels)")
    fig.suptitle(f"frame 0 of {sample.parent}  —  which is the real geometry?", fontsize=12)
    fig.tight_layout()
    png = HERE / "data" / "inferred_shape_check.png"
    fig.savefig(png, dpi=110)
    print(f"wrote {png}")
