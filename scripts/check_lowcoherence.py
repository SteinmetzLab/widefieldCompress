"""Four blocked archives had low row coherence even at 560x560. What is actually in them?"""

from __future__ import annotations

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

from infer_shape_v2 import read_frames, row_coherence  # noqa: E402

HERE = Path(__file__).resolve().parents[1]
BLOCK = 512

SUSPECT = [
    r"Y:\Subjects\AL_0033\2025-02-24\3\3.tar",
    r"Y:\Subjects\test\2025-12-03\1.tar",
    r"Y:\Subjects\test\2025-11-04\1.tar",
    r"Y:\Subjects\default\2025-03-05\3\widefield.tar",
    r"Y:\Subjects\test\2025-11-05\1\1.tar",   # high coherence, for contrast
]


def frame_at(tar: Path, target: int):
    """Frame at roughly `target` into the archive (constant stride assumed)."""
    with open(tar, "rb") as fh:
        h = fh.read(BLOCK)
        size = int(h[124:136].rstrip(b"\0 ").decode("ascii", "replace") or "0", 8)
        off = 0
        if size == 0:  # leading directory entry
            off = BLOCK
            fh.seek(off)
            h = fh.read(BLOCK)
            size = int(h[124:136].rstrip(b"\0 ").decode("ascii", "replace") or "0", 8)
        stride = BLOCK + ((size + 511) // 512) * 512
        n = (tar.stat().st_size - off) // stride
        k = min(target, max(n - 1, 0))
        fh.seek(off + k * stride + BLOCK)
        return np.frombuffer(fh.read(size), "<u2").reshape(560, 560), n


fig, axes = plt.subplots(2, len(SUSPECT), figsize=(3.2 * len(SUSPECT), 7))
for col, path in enumerate(SUSPECT):
    p = Path(path)
    for row, which in enumerate([0, 5000]):
        img, n = frame_at(p, which)
        ax = axes[row, col]
        ax.imshow(img, cmap="gray")
        ax.set_title(f"{p.parent.parts[-2]}/{p.parent.name}\nframe {min(which, n-1)} of {n:,}\n"
                     f"coh {row_coherence(img):.3f}  max {img.max()}", fontsize=8)
        ax.set_xlabel("x (pixels)" if row == 1 else "")
        ax.set_ylabel("y (pixels)" if col == 0 else "")
fig.suptitle("Low-row-coherence archives: is there any signal in them?", fontsize=12)
fig.tight_layout()
out = HERE / "data" / "lowcoherence_check.png"
fig.savefig(out, dpi=105)
print(f"wrote {out}")

for path in SUSPECT:
    p = Path(path)
    img0, n = frame_at(p, 0)
    imgm, _ = frame_at(p, n // 2)
    print(f"{p.parent.parts[-2]}/{p.parent.name:>12s}  {n:>7,d} frames  "
          f"frame0 max={img0.max():5d} coh={row_coherence(img0):.3f}   "
          f"mid max={imgm.max():5d} coh={row_coherence(imgm):.3f}")
