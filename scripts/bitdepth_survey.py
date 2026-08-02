"""How is the 16-bit word actually used, across the corpus? Samples 3 frames from ~90 tars."""

import csv
import io
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(
    0,
    r"C:\Users\nicks\AppData\Local\Temp\claude\D--Dropbox-code-widefieldCompress"
    r"\b9d8ffb2-d8d7-4255-af3d-c32ab501922b\scratchpad\pylibs",
)
import numpy as np
import tifffile

BLOCK = 512
rows = [r for r in csv.DictReader(Path("tar_census.csv").open(encoding="utf-8"))
        if int(r["bytes"]) > 0 and r["kind"] in ("frame-N", "basler-tiff", "other")]
random.seed(1)
sample = random.sample(rows, 90)


def probe(r):
    path, kind = r["path"], r["kind"]
    try:
        frames, off, n = [], 0, 0
        with open(path, "rb") as fh:
            while n < 3:
                fh.seek(off)
                h = fh.read(BLOCK)
                if not h or h[:1] == b"\0":
                    break
                size = int(h[124:136].rstrip(b"\0 ").decode(errors="replace") or "0", 8)
                if size:
                    raw = fh.read(size)
                    if raw[:2] in (b"II", b"MM"):
                        a = tifffile.imread(io.BytesIO(raw))
                    else:
                        side = int((size // 2) ** 0.5)
                        if side * side * 2 != size:
                            return None
                        a = np.frombuffer(raw, "<u2").reshape(side, side)
                    frames.append(np.asarray(a))
                    n += 1
                # skip ahead a few thousand frames so we don't only see the start
                off += (BLOCK + ((size + 511) // 512) * 512) * (1 if n < 2 else 5000)
        if not frames:
            return None
        v = np.concatenate([f.ravel() for f in frames]).astype(np.uint16)
        om = int(np.bitwise_or.reduce(v))
        lowz = ((om & -om).bit_length() - 1) if om else 0
        return dict(
            path=path, kind=kind, bytes=int(r["bytes"]), shape=f"{frames[0].shape}",
            dtype=str(frames[0].dtype), maxval=int(v.max()),
            low_zero=lowz, high_zero=16 - om.bit_length(),
            payload=om.bit_length() - lowz, ormask=f"{om:016b}",
        )
    except Exception:  # noqa: BLE001
        return None


with ThreadPoolExecutor(max_workers=12) as ex:
    out = [r for r in ex.map(probe, sample) if r]

print(f"probed {len(out)}/{len(sample)} tars\n")

from collections import Counter, defaultdict

combo = Counter((r["kind"], r["low_zero"], r["payload"]) for r in out)
tb = defaultdict(int)
for r in out:
    tb[(r["kind"], r["low_zero"], r["payload"])] += r["bytes"]
print(f"{'flavour':<14s}{'low bits zero':>14s}{'payload bits':>14s}{'n':>5s}{'sampled TB':>12s}")
print("-" * 60)
for (k, lz, pl), n in sorted(combo.items(), key=lambda x: (-x[1])):
    print(f"{k:<14s}{lz:>14d}{pl:>14d}{n:>5d}{tb[(k,lz,pl)]/1e12:>12.2f}")

shifted = [r for r in out if r["low_zero"] > 0]
print(f"\nleft-shifted (recoverable bits): {len(shifted)}/{len(out)} tars, "
      f"{sum(r['bytes'] for r in shifted)/1e12:.1f} TB of the {sum(r['bytes'] for r in out)/1e12:.1f} TB sampled")

print("\nshapes seen:", Counter(r["shape"] for r in out).most_common(8))

with open("bitdepth_survey.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=list(out[0].keys()))
    w.writeheader()
    w.writerows(out)
print("\nwrote bitdepth_survey.csv")
