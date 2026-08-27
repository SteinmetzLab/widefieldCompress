"""Lift one non-frame member out of a session tar and write it beside the session, verified.

Two of the five `UnsupportedArchive` sessions hold the only copy of
``1/p0.missed_samples.imec0.txt`` - a SpikeGLX dropped-sample log swept into the widefield tar
because the acquisition script tars the whole session directory. `codec.compress(drop_members=)`
rightly refuses to discard a member with no verified copy outside, so the log has to be preserved
before the archive can be compressed.

This copies the member out, hashes both the in-tar bytes and the written file, and refuses to leave
anything behind unless they match. It writes into the session's ephys directory, which is where the
three sibling sessions that already have this file keep it. Nothing is deleted and no tar is
modified.

    python scripts/extract_stray_member.py --session ZYE_0098/2026-01-02/1 \
        --member 1/p0.missed_samples.imec0.txt --dest-subdir p0_g0_t0.imec0
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))

READ = 1 << 20


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", required=True)
    ap.add_argument("--member", required=True)
    ap.add_argument("--dest-subdir", default="",
                    help="subdirectory of the session to write into; default the session root")
    ap.add_argument("--server", default=r"\\sahale.biostr.washington.edu\data")
    ap.add_argument("--tar-name", default="widefield.tar")
    ap.add_argument("--file-log", default=str(HERE / "data" / "fileEditLog.csv"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from wfcompress import filelog, tarwalk

    sess = args.session.strip("/").replace("\\", "/")
    sessdir = Path(args.server) / "Subjects" / Path(sess)
    tar = sessdir / args.tar_name
    if not tar.exists():
        print(f"no tar at {tar}")
        return 2

    # read_entries takes a path and parallelises the header walk; walk() wants an open handle
    entries = tarwalk.read_entries(tar)
    match = [e for e in entries if e.name == args.member]
    if len(match) != 1:
        print(f"expected exactly one member named {args.member!r}, found {len(match)}")
        return 2
    e = match[0]
    print(f"member  {e.name}\n  size  {e.size} B\n  at    offset {e.data_offset} in {tar.name}")

    # hash the bytes as they sit inside the tar
    h = hashlib.sha256()
    with open(tar, "rb") as fh:
        fh.seek(e.data_offset)
        left = e.size
        payload = bytearray()
        while left:
            chunk = fh.read(min(READ, left))
            if not chunk:
                print("unexpected end of file reading the member")
                return 2
            h.update(chunk)
            payload += chunk
            left -= len(chunk)
    inside = h.hexdigest()
    print(f"  in-tar sha256 {inside}")

    dest_dir = sessdir / args.dest_subdir if args.dest_subdir else sessdir
    dest = dest_dir / Path(args.member).name
    print(f"\ndestination {dest}")
    if not dest_dir.is_dir():
        print(f"  destination directory does not exist: {dest_dir}")
        return 2
    if dest.exists():
        existing = hashlib.sha256(dest.read_bytes()).hexdigest()
        if existing == inside:
            print("  already present with identical content - nothing to do")
            return 0
        print(f"  REFUSING: a different file is already there (sha256 {existing[:16]}...)")
        return 2
    if args.dry_run:
        print("  --dry-run, not writing")
        return 0

    tmp = dest.with_name(dest.name + ".partial-extract")
    tmp.write_bytes(bytes(payload))
    written = hashlib.sha256(tmp.read_bytes()).hexdigest()
    if written != inside:
        tmp.unlink()
        print(f"  REFUSING: written bytes hash {written[:16]}..., expected {inside[:16]}...")
        return 1
    tmp.replace(dest)
    print(f"  written and verified: sha256 matches, {dest.stat().st_size} B")
    filelog.record(args.file_log, "create", dest, e.size,
                   note=f"lifted from {args.tar_name} member {args.member}; sha256 {inside[:16]}")
    print("  recorded in the file log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
