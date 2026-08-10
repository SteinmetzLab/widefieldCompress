"""Mixed session archives: which members are frames, and is the rest safe to drop?

Twenty-one ``widefield.tar`` files on the share hold widefield frames *and* a whole SpikeGLX
recording, because the acquisition script tars the session directory rather than the frame list.
The plan is to keep only the frames. That is the first operation in this project that deliberately
discards data, so it does not happen on the strength of a filename.

For every non-frame member this proves an equivalent file exists outside the tar, by SHA-256 of
the bytes, and records where. :func:`wfcompress.codec.compress` refuses to drop anything that is
not in the resulting manifest.

Everything here is read-only.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from .. import tarwalk

BLOCK = tarwalk.BLOCK
READ_BLOCK = 1 << 24  # 16 MB; SMB throughput scales hard with read size


@dataclass
class Discardable:
    """One non-frame member, and the evidence for dropping it."""

    member: str
    member_bytes: int
    outside_path: str = ""
    outside_bytes: int = 0
    sha256: str = ""
    verified: bool = False
    method: str = ""           # "sha256", "size", or "" when nothing was found
    note: str = ""


@dataclass
class MixedArchive:
    tar: str
    tar_bytes: int = 0
    frame_bytes: int = 0        # size of one frame member
    n_frames: int = 0
    frames_total_bytes: int = 0
    discardable: list[Discardable] = field(default_factory=list)
    error: str = ""

    @property
    def all_verified(self) -> bool:
        return bool(self.discardable) and all(d.verified for d in self.discardable)

    @property
    def discard_bytes(self) -> int:
        return sum(d.member_bytes for d in self.discardable)

    def manifest(self) -> dict[str, dict]:
        """What ``compress(drop_non_frames=...)`` consumes: member name -> evidence."""
        return {d.member: asdict(d) for d in self.discardable if d.verified}


def partition(entries: list[tarwalk.Entry]) -> tuple[int, list[tarwalk.Entry],
                                                     list[tarwalk.Entry]]:
    """Split entries into (frame_size, frames, others).

    The frame size is the most common non-zero member size. Zero-size entries - the directory
    records tar writes - are neither: they carry no data, they are cheap to keep, and dropping
    them would change the shape of the rebuilt archive for no gain.
    """
    sizes = Counter(e.size for e in entries if e.size > 0)
    if not sizes:
        return 0, [], []
    frame_size = sizes.most_common(1)[0][0]
    frames = [e for e in entries if e.size == frame_size]
    others = [e for e in entries if e.size > 0 and e.size != frame_size]
    return frame_size, frames, others


def _hash_span(fh, offset: int, length: int) -> str:
    h = hashlib.sha256()
    fh.seek(offset)
    left = length
    while left:
        chunk = fh.read(min(READ_BLOCK, left))
        if not chunk:
            raise OSError(f"short read {length - left}/{length} at offset {offset}")
        h.update(chunk)
        left -= len(chunk)
    return h.hexdigest()


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(READ_BLOCK):
            h.update(chunk)
    return h.hexdigest()


@lru_cache(maxsize=64)
def _index(root: Path) -> dict[str, tuple[Path, ...]]:
    """basename -> every file with that name under ``root``.

    Built once per directory and cached. The naive alternative, an ``rglob`` per member, re-walks
    a subject folder five times per archive over SMB, which dominates everything else the check
    does.
    """
    out: dict[str, list[Path]] = {}
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir():
                    stack.append(e)
                else:
                    out.setdefault(e.name, []).append(e)
            except OSError:
                continue
    return {k: tuple(v) for k, v in out.items()}


def candidate_stages(member: str, tar: Path) -> list[list[Path]]:
    """Places the same file might live outside the archive, cheapest search first.

    The obvious guess is the path the member name implies, but it is often wrong: these sessions
    were reorganised after the tar was written, so ``1/p0_g0/p0_g0_imec0/x.ap.bin`` inside can be
    ``1/p0_g0_imec0/x.ap.bin`` on disk, and in one case the loose copy sits under a different
    session number entirely.

    Returned in stages so the caller can stop early. Indexing a whole subject folder means walking
    every session's sorter output over SMB, which is far more expensive than the date folder and
    almost never needed.
    """
    date_dir = tar.parent.parent        # .../Subjects/<subj>/<date>
    subject_dir = date_dir.parent
    name = Path(member.replace("\\", "/")).name
    implied = date_dir / member.replace("\\", "/")

    def under(root: Path, exclude: set[Path]) -> list[Path]:
        return [p for p in _index(root).get(name, ()) if p not in exclude and p != tar]

    seen = {implied}
    stage_date = under(date_dir, seen)
    seen.update(stage_date)
    return [[implied], stage_date, ["SUBJECT", subject_dir, name, seen]]  # type: ignore[list-item]


def candidates(member: str, tar: Path) -> list[Path]:
    """Every candidate, all stages. Convenience for reporting; the check uses the stages."""
    stages = candidate_stages(member, tar)
    out = list(stages[0]) + list(stages[1])
    _, subject_dir, name, seen = stages[2]
    out += [p for p in _index(subject_dir).get(name, ()) if p not in seen and p != tar]
    return out


def verify_member(entry: tarwalk.Entry, tar: Path, fh, quick: bool = False) -> Discardable:
    """Prove ``entry``'s bytes exist outside the tar. Hashes both copies unless ``quick``."""
    d = Discardable(member=entry.name, member_bytes=entry.size)

    # widen the search only until something of the right size turns up
    found: list[Path] = []
    stages = candidate_stages(entry.name, tar)
    for stage in stages:
        if stage and stage[0] == "SUBJECT":
            _, subject_dir, name, seen = stage
            paths = [p for p in _index(subject_dir).get(name, ()) if p not in seen and p != tar]
        else:
            paths = list(stage)
        found = [p for p in paths if _size_or_none(p) == entry.size]
        if found:
            break

    if not found:
        near = [str(p) for p in candidates(entry.name, tar) if p.is_file()]
        d.note = "no file of the same size outside the tar"
        if near:
            d.note += "; same-name candidates: " + ", ".join(near[:3])
        return d

    d.outside_path = str(found[0])
    d.outside_bytes = entry.size
    if quick:
        d.verified = True
        d.method = "size"
        d.note = "size match only - not proof of identical content"
        return d

    try:
        inside = _hash_span(fh, entry.data_offset, entry.size)
    except OSError as e:
        d.note = f"could not read the member: {e}"
        return d

    for p in found:
        try:
            if _hash_file(p) == inside:
                d.outside_path, d.sha256 = str(p), inside
                d.verified, d.method = True, "sha256"
                return d
        except OSError as e:
            d.note = f"{p}: {e}"
    d.note = (f"{len(found)} same-size candidate(s) outside, none matching sha256 {inside[:16]}...")
    return d


def _size_or_none(p: Path) -> int | None:
    try:
        return p.stat().st_size
    except OSError:
        return None


def inspect(tar_path: str | Path, quick: bool = False,
            progress=None) -> MixedArchive:
    """Enumerate one archive, partition it, and verify every discardable member."""
    tar = Path(tar_path)
    rec = MixedArchive(tar=str(tar))
    try:
        rec.tar_bytes = tar.stat().st_size
        # Bisect for where the frames stop, then walk only what follows. Enumerating every header
        # would be a quarter of a million round trips per archive to learn something the stride
        # already implies.
        probe, n_frames, after = tarwalk.uniform_prefix(tar)
        if probe is None:
            rec.error = "no data members"
            return rec
        others = [e for e in tarwalk.entries_after(tar, after) if e.size > 0]
    except (OSError, tarwalk.MalformedArchive) as e:
        rec.error = f"{type(e).__name__}: {e}"
        return rec

    rec.frame_bytes = probe.size
    rec.n_frames = n_frames
    rec.frames_total_bytes = probe.size * n_frames
    if not others:
        rec.error = "no non-frame members; this archive is not mixed"
        return rec

    with open(tar, "rb") as fh:
        for i, e in enumerate(others):
            if progress:
                progress(i, len(others), e)
            rec.discardable.append(verify_member(e, tar, fh, quick=quick))
    return rec


def write_manifest(records: list[MixedArchive], path: str | Path) -> Path:
    """One JSON keyed by archive path; consumed by the batch driver."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {
        r.tar: {
            "tar_bytes": r.tar_bytes,
            "frame_bytes": r.frame_bytes,
            "n_frames": r.n_frames,
            "all_verified": r.all_verified,
            "discard_bytes": r.discard_bytes,
            "discardable": [asdict(d) for d in r.discardable],
            "error": r.error,
        }
        for r in records
    }
    path.write_text(json.dumps(blob, indent=1, sort_keys=True), encoding="utf-8")
    return path


def read_manifest(path: str | Path) -> dict[str, dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verified_members(manifest: dict, tar_path: str | Path) -> dict[str, dict]:
    """The member -> evidence mapping for one archive, or {} if it is not fully verified."""
    import os

    key = os.path.normcase(os.path.realpath(str(tar_path)))
    for k, v in manifest.items():
        if os.path.normcase(os.path.realpath(k)) == key:
            if not v.get("all_verified"):
                return {}
            return {d["member"]: d for d in v["discardable"] if d.get("verified")}
    return {}
