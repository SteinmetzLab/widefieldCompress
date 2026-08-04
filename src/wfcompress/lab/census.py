"""Inventory of widefield tars on the lab shares.

Two passes, both read-only:

``find_tars``   walk the Subjects tree for ``*.tar`` (slow over SMB; minutes)
``probe``       read ~1.5 KB from the head of each tar to classify it (parallel; fast)

The result is a CSV that the batch driver consumes. Re-run it immediately before any bulk job;
new sessions land continuously.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import tifffile

from .session import has_svd, session_frame_shape

log = logging.getLogger(__name__)

BLOCK = 512

# Y: sahale, Z: steinmetzsuper1. Z is nearly full and currently out of scope.
DEFAULT_ROOTS = {
    "Y": Path(r"\\sahale.biostr.washington.edu\data\Subjects"),
    "Z": Path(r"\\steinmetzsuper1.biostr.washington.edu\data"),
}

CSV_FIELDS = [
    "server", "path", "bytes", "kind", "member_bytes", "est_frames",
    "rows", "cols", "shift", "payload_bits", "maxval", "has_svd", "first_member", "error",
]


@dataclass
class TarRecord:
    server: str
    path: str
    bytes: int
    kind: str = ""
    member_bytes: int = 0
    est_frames: int = 0
    rows: int = 0
    cols: int = 0
    shift: int = -1
    payload_bits: int = 0
    maxval: int = 0
    has_svd: bool = False
    first_member: str = ""
    error: str = ""


@dataclass
class Census:
    records: list[TarRecord] = field(default_factory=list)

    @property
    def widefield(self) -> list[TarRecord]:
        return [r for r in self.records if r.kind in ("frame-N", "basler-tiff") and r.bytes > 0]

    def total_bytes(self, kinds: tuple[str, ...] | None = None) -> int:
        rs = self.records if kinds is None else [r for r in self.records if r.kind in kinds]
        return sum(r.bytes for r in rs)

    def write_csv(self, path: str | Path) -> Path:
        path = Path(path)
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in self.records:
                w.writerow(asdict(r))
        return path

    @classmethod
    def read_csv(cls, path: str | Path) -> Census:
        rows = []
        with Path(path).open(encoding="utf-8") as fh:
            for d in csv.DictReader(fh):
                rows.append(
                    TarRecord(
                        server=d["server"], path=d["path"], bytes=int(d["bytes"] or 0),
                        kind=d["kind"], member_bytes=int(d["member_bytes"] or 0),
                        est_frames=int(d["est_frames"] or 0), rows=int(d["rows"] or 0),
                        cols=int(d["cols"] or 0), shift=int(d["shift"] or -1),
                        payload_bits=int(d["payload_bits"] or 0), maxval=int(d["maxval"] or 0),
                        has_svd=d["has_svd"] in ("True", "true", "1"),
                        first_member=d["first_member"], error=d["error"],
                    )
                )
        return cls(rows)


def classify(name: str) -> str:
    if re.fullmatch(r"(.*/)?frame-\d+", name):
        return "frame-N"
    if "Basler" in name and name.endswith((".tif", ".tiff")):
        return "basler-tiff"
    if name.endswith(".ome.tif"):
        return "ome-tif"
    if name.endswith((".tif", ".tiff")):
        return "other-tiff"
    return "other"


def find_tars(
    root: str | Path, pattern: str = "*.tar", max_depth: int = 5,
    errors: list[str] | None = None,
) -> list[Path]:
    """Depth-limited walk for tar files. Slow over SMB; expect minutes on a big share.

    Directories that cannot be read are appended to ``errors`` rather than skipped silently. A
    transient SMB or permission failure would otherwise drop a whole subtree while the census
    still looked complete, which is exactly the kind of gap that makes a deletion decision unsafe.
    """
    root = Path(root)
    found: list[Path] = []
    errors = errors if errors is not None else []

    def walk(d: Path, depth: int):
        if depth > max_depth:
            return
        try:
            entries = list(d.iterdir())
        except OSError as e:
            errors.append(f"{d}: {type(e).__name__}: {e}")
            return
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError as e:
                errors.append(f"{entry}: {type(e).__name__}: {e}")
                continue
            if is_dir:
                walk(entry, depth + 1)
            elif entry.match(pattern):
                found.append(entry)

    walk(root, 0)
    return found


def probe(path: str | Path, server: str = "", sample_frames: int = 3) -> TarRecord:
    """Classify one tar and sniff its geometry and bit usage. Reads a few frames only."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as e:
        return TarRecord(server=server, path=str(path), bytes=0, kind="ERROR", error=str(e))

    rec = TarRecord(server=server, path=str(path), bytes=size, has_svd=has_svd(path))
    if size == 0:
        rec.kind = "EMPTY"
        return rec

    try:
        shape = session_frame_shape(path)
        arrays, off, seen = [], 0, 0
        with open(path, "rb") as fh:
            while seen < sample_frames:
                fh.seek(off)
                h = fh.read(BLOCK)
                if len(h) < BLOCK or h[:1] == b"\0":
                    break
                member = h[:100].rstrip(b"\0").decode("utf-8", "replace")
                msize = int(h[124:136].rstrip(b"\0 ").decode("ascii", "replace") or "0", 8)
                if msize:
                    if not rec.first_member:
                        rec.first_member = member
                        rec.kind = classify(member)
                        rec.member_bytes = msize
                        stride = BLOCK + ((msize + 511) // 512) * 512
                        rec.est_frames = (size - (off)) // stride
                    raw = fh.read(msize)
                    if raw[:2] in (b"II", b"MM"):
                        arrays.append(np.asarray(tifffile.imread(io.BytesIO(raw))))
                    elif shape and shape[0] * shape[1] * 2 == msize:
                        arrays.append(np.frombuffer(raw, "<u2").reshape(shape))
                    seen += 1
                    off += (BLOCK + ((msize + 511) // 512) * 512) * (1 if seen < 2 else 5000)
                else:
                    off += BLOCK
        if arrays:
            rec.rows, rec.cols = arrays[0].shape
            v = np.concatenate([a.ravel() for a in arrays]).astype(np.uint16)
            om = int(np.bitwise_or.reduce(v))
            rec.maxval = int(v.max())
            rec.shift = ((om & -om).bit_length() - 1) if om else 0
            rec.payload_bits = om.bit_length() - rec.shift
        elif rec.kind == "frame-N":
            rec.error = "headerless frames and no meanImage.npy for geometry"
    except Exception as e:  # noqa: BLE001 - one bad archive must not kill the census
        rec.error = f"{type(e).__name__}: {e}"
    return rec


def scan(roots: dict[str, Path] | None = None, workers: int = 16,
         strict: bool = True) -> Census:
    """Full inventory across the given roots.

    ``strict`` raises when any directory could not be traversed. Leave it on for anything feeding
    a deletion decision: a census that quietly omits a subtree is worse than no census.
    """
    roots = roots or DEFAULT_ROOTS
    jobs: list[tuple[str, Path]] = []
    errors: list[str] = []
    for server, root in roots.items():
        for p in find_tars(root, errors=errors):
            jobs.append((server, p))
    if errors:
        msg = f"{len(errors)} director{'y' if len(errors) == 1 else 'ies'} could not be read"
        detail = "\n  ".join(errors[:20])
        if strict:
            raise OSError(f"{msg}:\n  {detail}")
        log.warning("%s; the census is incomplete:\n  %s", msg, detail)
    with ThreadPoolExecutor(workers) as ex:
        records = list(ex.map(lambda j: probe(j[1], j[0]), jobs))
    return Census(records)
