"""Deciding whether an original tar is safe to delete. Read-only; nothing here removes anything.

The whole project has been built so that this one decision can be made on evidence rather than on
trust. A tar is a deletion candidate only if all of the following hold **right now**, not at some
point in the past:

1. a ``.wfz`` exists beside it, of the size the run log recorded;
2. the ``.wfz`` is a format this build can read, and carries a ``source_tar_sha256``;
3. the tar on disk is still the size that hash was taken over -- this is the guard that matters
   most, because the tar changing after compression is not hypothetical: all 21 mixed session
   archives were re-tarred days after being censused;
4. a receipt sits beside the ``.wfz`` recording that its rebuild was verified byte-identical;
5. and, for the strict tier, the ``.wfz`` reproduces that hash *today* -- proving it has not
   rotted since -- and optionally the tar re-hashes to the same value, closing the loop.

Tiers 1-4 are metadata only and take seconds for the whole corpus. Tier 5 costs a full read.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import codec, container

#: Oldest format this is willing to delete an original for. Version 1 predates the temporal-order
#: map and the source hash; those files must be rewritten before their tar goes.
MIN_FORMAT_VERSION = 2

FIELDS = [
    "tar", "wfz", "session", "verdict", "reason",
    "tar_bytes_now", "tar_bytes_recorded", "wfz_bytes_now", "wfz_bytes_recorded",
    "format_version", "has_receipt", "receipt_byte_identical", "source_tar_sha256",
    "reverified", "reverified_sha256", "tar_rehash", "tar_rehash_matches",
]


@dataclass
class Candidate:
    tar: str
    wfz: str = ""
    session: str = ""
    verdict: str = "REFUSE"          # KEEP-verdicts are never implied by absence
    reason: str = ""
    tar_bytes_now: int = 0
    tar_bytes_recorded: int = 0
    wfz_bytes_now: int = 0
    wfz_bytes_recorded: int = 0
    format_version: int = 0
    has_receipt: bool = False
    receipt_byte_identical: bool = False
    source_tar_sha256: str = ""
    reverified: bool = False
    reverified_sha256: str = ""
    tar_rehash: str = ""
    tar_rehash_matches: bool = False


@dataclass
class Audit:
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def safe(self) -> list[Candidate]:
        return [c for c in self.candidates if c.verdict == "SAFE"]

    @property
    def refused(self) -> list[Candidate]:
        return [c for c in self.candidates if c.verdict != "SAFE"]

    def write_csv(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for c in self.candidates:
                w.writerow(asdict(c))
        return path


def inspect(record: dict, strict: bool = False, rehash_tar: bool = False,
            threads: int = 8) -> Candidate:
    """Judge one logged success. ``record`` is a row from the batch driver's jsonl log."""
    tar, wfz = Path(record["tar"]), Path(record["wfz"])
    c = Candidate(tar=str(tar), wfz=str(wfz), session=record.get("session", ""),
                  tar_bytes_recorded=int(record.get("source_bytes") or 0),
                  wfz_bytes_recorded=int(record.get("output_bytes") or 0))

    def refuse(why: str) -> Candidate:
        c.reason = why
        return c

    if not record.get("ok"):
        return refuse("the run log does not record this archive as successful")
    if not tar.is_file():
        return refuse("the tar is already gone")
    c.tar_bytes_now = tar.stat().st_size
    if not wfz.is_file():
        return refuse("no .wfz beside the tar")
    c.wfz_bytes_now = wfz.stat().st_size

    if c.wfz_bytes_recorded and c.wfz_bytes_now != c.wfz_bytes_recorded:
        return refuse(f".wfz is {c.wfz_bytes_now:,} B, the log recorded "
                      f"{c.wfz_bytes_recorded:,} B")

    try:
        meta = container.read_meta(wfz)
    except Exception as e:  # noqa: BLE001 - an unreadable footer is itself the answer
        return refuse(f"cannot read the .wfz footer: {type(e).__name__}: {e}")

    c.format_version = int(meta.get("format_version", 0))
    if c.format_version < MIN_FORMAT_VERSION:
        return refuse(f"format version {c.format_version} predates the recorded source hash; "
                      f"recompress before deleting")
    if meta.get("partial"):
        return refuse("this .wfz deliberately holds only part of the archive")

    c.source_tar_sha256 = meta.get("source_tar_sha256") or ""
    if not c.source_tar_sha256:
        return refuse("the .wfz carries no source_tar_sha256, so byte-identity cannot be shown")

    # The guard that matters most. Compression hashed the tar as it read it; if the file on disk is
    # no longer that size, the hash describes something that is no longer there.
    if c.tar_bytes_recorded and c.tar_bytes_now != c.tar_bytes_recorded:
        return refuse(f"the tar is {c.tar_bytes_now:,} B but was {c.tar_bytes_recorded:,} B when "
                      f"it was compressed - it has been replaced since")

    receipt = wfz.with_name(wfz.name + ".receipt.json")
    c.has_receipt = receipt.is_file()
    if not c.has_receipt:
        return refuse("no receipt beside the .wfz; verification never completed")
    try:
        r = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return refuse(f"unreadable receipt: {e}")
    c.receipt_byte_identical = bool(r.get("byte_identical"))
    if not c.receipt_byte_identical:
        return refuse("the receipt does not claim a byte-identical rebuild")

    if strict:
        try:
            v = codec.verify(wfz, threads=threads)
        except Exception as e:  # noqa: BLE001
            return refuse(f"re-verification failed: {type(e).__name__}: {e}")
        c.reverified = bool(v.get("byte_identical"))
        c.reverified_sha256 = v.get("tar_sha256", "")
        if not c.reverified:
            return refuse("the .wfz no longer reproduces its recorded hash")
        if rehash_tar:
            c.tar_rehash = codec.sha256_file(tar)
            c.tar_rehash_matches = c.tar_rehash == c.source_tar_sha256
            if not c.tar_rehash_matches:
                return refuse("the tar on disk no longer hashes to the value recorded at "
                              "compression time")

    c.verdict = "SAFE"
    c.reason = ("metadata checks pass" if not strict else
                "re-verified today" + (" and the tar re-hashes to match" if rehash_tar else ""))
    return c
