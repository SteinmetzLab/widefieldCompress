"""Inventory of raw SpikeGLX ephys on the lab share, and what compressing it would save.

The widefield campaign is nearly scoped out; the next big consumer is uncompressed
``*.ap.bin`` / ``*.lf.bin`` / ``*.nidq.bin``. This walks the tree, records every one, and notes
whether an ``mtscomp``-style ``.cbin``/``.ch`` pair already exists beside it - a file that is
already compressed is not a saving available twice.

Read-only. The walk is parallel because a depth-7 traversal of the Subjects tree over SMB is
dominated by round trips, not by anything local.
"""

from __future__ import annotations

import csv
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: SpikeGLX writes these three flavours. `.nidq` is small; `.lf` is 1/12 the rate of `.ap` but
#: still adds up. All are flat int16, which is what mtscomp expects.
RAW_SUFFIXES = (".ap.bin", ".lf.bin", ".nidq.bin")
COMPRESSED_SUFFIXES = (".ap.cbin", ".lf.cbin", ".nidq.cbin")

CSV_FIELDS = [
    "server", "path", "bytes", "band", "subject", "date", "session",
    "has_cbin", "cbin_bytes", "has_ch", "has_meta", "n_channels", "sample_rate_hz",
    "implied_seconds", "error",
]


@dataclass
class EphysFile:
    server: str
    path: str
    bytes: int
    band: str = ""
    subject: str = ""
    date: str = ""
    session: str = ""
    has_cbin: bool = False
    cbin_bytes: int = 0
    has_ch: bool = False
    has_meta: bool = False
    n_channels: int = 0
    sample_rate_hz: float = 0.0
    implied_seconds: float = 0.0
    error: str = ""


@dataclass
class EphysCensus:
    records: list[EphysFile] = field(default_factory=list)

    def write_csv(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in self.records:
                w.writerow(asdict(r))
        return path

    @classmethod
    def read_csv(cls, path: str | Path) -> EphysCensus:
        out = []
        with Path(path).open(encoding="utf-8") as fh:
            for d in csv.DictReader(fh):
                out.append(EphysFile(
                    server=d["server"], path=d["path"], bytes=int(d["bytes"] or 0),
                    band=d["band"], subject=d["subject"], date=d["date"], session=d["session"],
                    has_cbin=d["has_cbin"] == "True", cbin_bytes=int(d["cbin_bytes"] or 0),
                    has_ch=d["has_ch"] == "True", has_meta=d["has_meta"] == "True",
                    n_channels=int(d["n_channels"] or 0),
                    sample_rate_hz=float(d["sample_rate_hz"] or 0),
                    implied_seconds=float(d["implied_seconds"] or 0), error=d["error"],
                ))
        return cls(out)


def band_of(name: str) -> str:
    for s in RAW_SUFFIXES:
        if name.endswith(s):
            return s[1:-4]  # "ap", "lf", "nidq"
    return ""


def parse_meta(path: Path) -> tuple[int, float]:
    """(n_channels, sample_rate) from a SpikeGLX ``.meta``. Zeros if it cannot be read.

    The two fields matter because mtscomp needs them to reshape the flat file, and because they
    let the census sanity-check a file's duration rather than trusting its size alone.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0.0
    n, rate = 0, 0.0
    for line in text.splitlines():
        if line.startswith("nSavedChans="):
            n = int(line.split("=", 1)[1].strip() or 0)
        elif re.match(r"^(imSampRate|niSampRate)=", line):
            rate = float(line.split("=", 1)[1].strip() or 0)
    return n, rate


def walk_parallel(root: Path, max_depth: int = 7, workers: int = 32,
                  errors: list[str] | None = None, progress=None,
                  keep: tuple[str, ...] = RAW_SUFFIXES) -> list[Path]:
    """Files under ``root`` matching ``keep``, listing directories concurrently, level by level.

    This share is 576 subjects and 8,725 date folders deep and the walk is pure latency, so the
    thread count sits well above the core count. Not arbitrarily far above, though: 96 threads
    alongside the compression job's eight workers wedged the SMB client - no progress in
    twenty-five minutes - where 32 runs cleanly. The concurrency limit here is the connection, not
    the CPU.

    Only matching files are retained. Keeping every path costs hundreds of MB on a tree this size
    and buys nothing - a sorter output directory can hold tens of thousands of files.

    Unreadable directories are recorded rather than silently skipped: a census that quietly omits
    a subtree is worse than no census when it feeds a deletion decision.
    """
    errors = errors if errors is not None else []
    files: list[Path] = []
    level = [root]

    def listdir(d: Path) -> tuple[list[Path], list[Path]]:
        # os.scandir, not Path.iterdir: iterdir yields plain paths and every `.is_dir()` on one is
        # a fresh stat call. Over SMB under load those measured about half a second each, which
        # turned listing the 576-entry Subjects root into a five-minute operation. scandir carries
        # the entry type in the directory listing itself, so the whole level costs one round trip.
        subdirs, plain = [], []
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            subdirs.append(Path(e.path))
                        elif e.name.endswith(keep):
                            plain.append(Path(e.path))
                    except OSError as ex:
                        errors.append(f"{e.path}: {type(ex).__name__}: {ex}")
        except OSError as ex:
            errors.append(f"{d}: {type(ex).__name__}: {ex}")
        return subdirs, plain

    for depth in range(max_depth + 1):
        if not level:
            break
        nxt: list[Path] = []
        with ThreadPoolExecutor(workers) as ex:
            for subdirs, plain in ex.map(listdir, level):
                nxt.extend(subdirs)
                files.extend(plain)
        # A depth-7 walk of this share takes tens of minutes over SMB, and without this the
        # caller cannot tell a slow level from a hang.
        if progress:
            progress(depth, len(level), len(nxt), len(files))
        level = nxt
    return files


def session_parts(path: Path, root: Path) -> tuple[str, str, str]:
    """(subject, date, session) from the Subjects/<subj>/<date>/<n>/... convention."""
    try:
        rel = path.relative_to(root).parts
    except ValueError:
        return "", "", ""
    subject = rel[0] if len(rel) > 0 else ""
    date = rel[1] if len(rel) > 1 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", rel[1]) else ""
    session = rel[2] if len(rel) > 2 and date else ""
    return subject, date, session


def describe(path: Path, root: Path, server: str) -> EphysFile:
    rec = EphysFile(server=server, path=str(path), bytes=0, band=band_of(path.name))
    rec.subject, rec.date, rec.session = session_parts(path, root)
    try:
        rec.bytes = path.stat().st_size
    except OSError as e:
        rec.error = f"{type(e).__name__}: {e}"
        return rec

    stem = path.name[: -len(".bin")]
    cbin = path.with_name(stem + ".cbin")
    ch = path.with_name(stem + ".ch")
    meta = path.with_name(stem + ".meta")
    try:
        if cbin.is_file():
            rec.has_cbin = True
            rec.cbin_bytes = cbin.stat().st_size
        rec.has_ch = ch.is_file()
        if meta.is_file():
            rec.has_meta = True
            rec.n_channels, rec.sample_rate_hz = parse_meta(meta)
            if rec.n_channels and rec.sample_rate_hz:
                rec.implied_seconds = rec.bytes / (2 * rec.n_channels * rec.sample_rate_hz)
    except OSError as e:
        rec.error = f"{type(e).__name__}: {e}"
    return rec


def scan(root: Path, server: str = "Y", workers: int = 32,
         strict: bool = False, progress=None) -> tuple[EphysCensus, list[str]]:
    errors: list[str] = []
    raw = walk_parallel(root, errors=errors, workers=workers, progress=progress)
    with ThreadPoolExecutor(workers) as ex:
        records = list(ex.map(lambda p: describe(p, root, server), raw))
    if errors and strict:
        raise OSError(f"{len(errors)} directories unreadable:\n  " + "\n  ".join(errors[:20]))
    return EphysCensus(records), errors
