"""Run wfcompress over many sessions, resumably.

Deliberate defaults: **originals are never deleted** unless ``--delete`` is passed, and each
session is verified by a full decompress-and-compare before its receipt records success. A run
can be interrupted at any point and restarted; completed sessions are skipped.

    python -m wfcompress.lab.batch --census tar_census.csv --limit 10
    python -m wfcompress.lab.batch --census tar_census.csv --jobs 6 --threads 8
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .. import codec, filelog, sidecar
from .census import Census
from .session import session_frame_shape, session_id


def process_one(
    tar_path: Path,
    out_path: Path,
    verify_full: bool = True,
    threads: int = 8,
    keep_restored: bool = False,
    min_age_s: float = codec.DEFAULT_MIN_AGE_S,
    file_log: str | None = None,
    assume_shape: tuple[int, int] | None = None,
) -> dict:
    """Compress one session and prove it round-trips. Never deletes anything."""
    t0 = time.perf_counter()
    result: dict = {"tar": str(tar_path), "wfz": str(out_path), "session": session_id(tar_path)}

    shape = session_frame_shape(tar_path)
    if shape is None and assume_shape is not None:
        # Only ever a fallback: a shape resolved from the session folder always wins. Used for the
        # handful of headerless archives with no meanImage.npy, where the geometry was established
        # separately (row-coherence analysis plus visual inspection of a rebuilt frame).
        shape = assume_shape
        result["shape_assumed"] = list(assume_shape)
    meta = codec.compress(tar_path, out_path, shape=shape, threads=threads,
                          min_age_s=min_age_s, file_log=file_log)
    result.update(
        ratio=meta["ratio"],
        source_bytes=meta["source_bytes"],
        output_bytes=meta["output_bytes"],
        shift=meta["shift"],
        n_frames=meta["n_frames"],
        shape=meta["shape"],
        is_tiff=meta["is_tiff"],
    )

    if verify_full:
        if keep_restored:
            # only when someone wants the rebuilt archive on disk to look at; costs the full
            # uncompressed size in writes plus two passes to hash both files
            restored = out_path.with_suffix(out_path.suffix + ".restored.tar")
            codec.decompress(out_path, restored, threads=threads, file_log=file_log)
            a, b = codec.sha256_file(tar_path), codec.sha256_file(restored)
            result.update(tar_sha256=a, restored_sha256=b, byte_identical=a == b)
            if a != b:
                raise codec.LosslessCheckFailed(f"{tar_path}: restored archive differs")
        else:
            # stream the reconstruction through SHA-256 and compare against the hash taken from
            # the source during compression -- same guarantee, a fraction of the I/O
            v = codec.verify(out_path, threads=threads)
            result.update(
                tar_sha256=v["tar_sha256"],
                byte_identical=v["byte_identical"],
                verified_by="stream",
            )

    # only now is byte-identity a fact rather than an expectation
    meta["byte_identical_verified"] = bool(result.get("byte_identical"))

    result["elapsed_s"] = time.perf_counter() - t0
    result["ok"] = True
    result["output_bytes"] = out_path.stat().st_size
    sidecar.write_readme(out_path, meta, file_log=file_log)
    sidecar.write_preview_frame(out_path, file_log=file_log)
    sidecar.write_receipt(out_path, meta,
                          extra={k: result[k] for k in ("tar_sha256", "byte_identical")
                                 if k in result},
                          file_log=file_log)
    return result


def canonical(path: str | Path) -> str:
    """A comparison key that survives the same file being named different ways.

    The share is reachable both as a mapped drive and as a UNC path, and the census and an older
    log can disagree about which. ``realpath`` resolves ``Y:\\...`` to
    ``\\\\sahale...\\data\\...``, so without this a regenerated census silently redoes work that
    was already finished - on a 16-day run, expensively.
    """
    return os.path.normcase(os.path.realpath(str(path)))


def _already_done(rec: dict) -> bool:
    """Whether a logged success can be trusted without redoing the work.

    A log line saying ``ok`` is not enough on its own: the output may have been moved, truncated
    or replaced since. For a workflow whose end state is deleting the originals, resume has to
    re-check that the artifact it is skipping still exists and is the size that was recorded.
    """
    if not rec.get("ok"):
        return False
    wfz = Path(rec.get("wfz", ""))
    if not wfz.is_file():
        return False
    expected = rec.get("output_bytes")
    return expected is None or wfz.stat().st_size == expected


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--census", required=True, help="CSV from wfcompress.lab.census")
    p.add_argument("--out-dir", help="write .wfz here instead of beside each tar")
    p.add_argument("--server", default="Y", help="restrict to one server tag (default Y)")
    p.add_argument("--limit", type=int, help="process at most N sessions")
    p.add_argument("--min-gb", type=float, default=0.0)
    p.add_argument("--max-gb", type=float, default=1e9)
    p.add_argument("--kind", action="append", help="restrict to a flavour; repeatable")
    p.add_argument("--largest-first", action="store_true")
    p.add_argument("--threads", type=int, default=8, help="threads within one session")
    p.add_argument("--jobs", type=int, default=1,
                   help="sessions to process concurrently, in separate processes. Worth ~2x over "
                        "spending the same cores on --threads, because the numpy work around the "
                        "codec holds the GIL")
    p.add_argument("--no-verify-full", action="store_true",
                   help="skip the decompress-and-compare pass (not recommended)")
    p.add_argument("--keep-restored", action="store_true",
                   help="leave the restored tar on disk for inspection")
    p.add_argument("--min-age-s", type=float, default=codec.DEFAULT_MIN_AGE_S,
                   help="skip archives modified more recently than this; data arrives here "
                        "straight off an acquisition machine and can be mid-transfer "
                        "(default 3600, use 0 to disable)")
    p.add_argument("--file-log", default="fileEditLog.csv",
                   help="append-only CSV recording every file created, replaced or removed on "
                        "disk, for auditing inside the 60-day recovery window")
    p.add_argument("--assume-shape", type=int, nargs=2, metavar=("ROWS", "COLS"),
                   help="frame geometry to use ONLY for archives where it cannot be resolved from "
                        "the session folder; never overrides a known shape")
    p.add_argument("--log", default="batch_log.jsonl")
    args = p.parse_args(argv)

    file_log = filelog.ensure(args.file_log) if args.file_log else None
    assume_shape = tuple(args.assume_shape) if args.assume_shape else None
    if assume_shape:
        print(f"geometry fallback for unresolvable archives: {assume_shape[0]}x{assume_shape[1]}")

    census = Census.read_csv(args.census)
    todo = [
        r for r in census.widefield
        if (not args.server or r.server == args.server)
        and args.min_gb * 1e9 <= r.bytes <= args.max_gb * 1e9
        and (not args.kind or r.kind in args.kind)
    ]
    todo.sort(key=lambda r: -r.bytes if args.largest_first else r.bytes)

    log_path = Path(args.log)
    done, stale = set(), 0
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not rec.get("ok"):
                continue
            if _already_done(rec):
                done.add(canonical(rec["tar"]))
            else:
                stale += 1
                done.discard(canonical(rec["tar"]))
    if stale:
        print(f"{stale} logged successes have a missing or wrong-sized output; redoing those")
    todo = [r for r in todo if canonical(r.path) not in done]
    if args.limit:
        todo = todo[: args.limit]

    print(f"{len(todo)} sessions to process ({sum(r.bytes for r in todo)/1e12:.2f} TB), "
          f"{len(done)} already done")

    def out_path_for(tar_path: Path) -> Path:
        out_dir = Path(args.out_dir) if args.out_dir else tar_path.parent
        stem = tar_path.stem
        if args.out_dir:
            stem = f"{session_id(tar_path).replace('/', '_')}_{stem}"
        return out_dir / f"{stem}.wfz"

    def failure(tar_path: Path, out_path: Path, e: BaseException) -> dict:
        return {
            "tar": str(tar_path), "wfz": str(out_path), "session": session_id(tar_path),
            "ok": False, "error": f"{type(e).__name__}: {e}",
            "traceback": "".join(traceback.format_exception(type(e), e, e.__traceback__)),
        }

    def record(result: dict, n: int) -> bool:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result) + "\n")
        tag = f"[{n}/{len(todo)}] {result['session']}"
        if result.get("ok"):
            print(f"{tag}  x{result['ratio']:.2f}  shift={result['shift']}  "
                  f"{'byte-identical' if result.get('byte_identical') else 'pixels verified'}  "
                  f"{result['elapsed_s']/60:.1f} min", flush=True)
        else:
            print(f"{tag}  FAILED: {result['error']}", flush=True)
        return bool(result.get("ok"))

    t_start = time.perf_counter()
    ok = 0

    if args.jobs > 1:
        # Sessions are independent, and threads inside one session scale poorly because the numpy
        # work around the codec holds the GIL. Running whole sessions in separate processes is
        # worth ~2x over the same core count spent on more threads.
        print(f"running {args.jobs} sessions concurrently, {args.threads} threads each")
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {}
            for rec in todo:
                tar_path = Path(rec.path)
                futures[pool.submit(
                    process_one, tar_path, out_path_for(tar_path),
                    not args.no_verify_full, args.threads, args.keep_restored,
                    args.min_age_s, str(file_log) if file_log else None, assume_shape,
                )] = tar_path
            for n, fut in enumerate(as_completed(futures), 1):
                tar_path = futures[fut]
                try:
                    ok += record(fut.result(), n)
                except Exception as e:  # noqa: BLE001 - one bad session must not stop the run
                    record(failure(tar_path, out_path_for(tar_path), e), n)
    else:
        for n, rec in enumerate(todo, 1):
            tar_path = Path(rec.path)
            out_path = out_path_for(tar_path)
            print(f"\n[{n}/{len(todo)}] {session_id(tar_path)}  {rec.bytes/1e9:.1f} GB  "
                  f"{rec.kind}  -> {out_path.name}", flush=True)
            try:
                ok += record(process_one(
                    tar_path, out_path,
                    verify_full=not args.no_verify_full,
                    threads=args.threads,
                    keep_restored=args.keep_restored,
                    min_age_s=args.min_age_s,
                    file_log=str(file_log) if file_log else None,
                    assume_shape=assume_shape,
                ), n)
            except Exception as e:  # noqa: BLE001
                record(failure(tar_path, out_path, e), n)

    elapsed = time.perf_counter() - t_start
    done_bytes = sum(r.bytes for r in todo)
    print(f"\n{ok}/{len(todo)} succeeded in {elapsed/3600:.2f} h "
          f"({done_bytes/1e6/elapsed:.0f} MB/s aggregate); log in {log_path}")
    return 0 if ok == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
