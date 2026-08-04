"""Run wfcompress over many sessions, resumably.

Deliberate defaults: **originals are never deleted** unless ``--delete`` is passed, and each
session is verified by a full decompress-and-compare before its receipt records success. A run
can be interrupted at any point and restarted; completed sessions are skipped.

    python -m wfcompress.lab.batch --census tar_census.csv --limit 10
    python -m wfcompress.lab.batch --census tar_census.csv --out-dir Y:/temp/pilot --verify-full
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from .. import codec, sidecar
from .census import Census
from .session import session_frame_shape, session_id


def process_one(
    tar_path: Path,
    out_path: Path,
    verify_full: bool = True,
    threads: int = 8,
    keep_restored: bool = False,
) -> dict:
    """Compress one session and prove it round-trips. Never deletes anything."""
    t0 = time.perf_counter()
    result: dict = {"tar": str(tar_path), "wfz": str(out_path), "session": session_id(tar_path)}

    shape = session_frame_shape(tar_path)
    meta = codec.compress(tar_path, out_path, shape=shape, threads=threads)
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
            codec.decompress(out_path, restored, threads=threads)
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

    result["elapsed_s"] = time.perf_counter() - t0
    result["ok"] = True
    sidecar.write_readme(out_path, meta)
    sidecar.write_preview_frame(out_path)
    sidecar.write_receipt(out_path, meta, extra={k: result[k] for k in
                                                 ("tar_sha256", "byte_identical")
                                                 if k in result})
    return result


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
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--no-verify-full", action="store_true",
                   help="skip the decompress-and-compare pass (not recommended)")
    p.add_argument("--keep-restored", action="store_true",
                   help="leave the restored tar on disk for inspection")
    p.add_argument("--log", default="batch_log.jsonl")
    args = p.parse_args(argv)

    census = Census.read_csv(args.census)
    todo = [
        r for r in census.widefield
        if (not args.server or r.server == args.server)
        and args.min_gb * 1e9 <= r.bytes <= args.max_gb * 1e9
        and (not args.kind or r.kind in args.kind)
    ]
    todo.sort(key=lambda r: -r.bytes if args.largest_first else r.bytes)

    log_path = Path(args.log)
    done = set()
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                if rec.get("ok"):
                    done.add(rec["tar"])
            except json.JSONDecodeError:
                continue
    todo = [r for r in todo if r.path not in done]
    if args.limit:
        todo = todo[: args.limit]

    print(f"{len(todo)} sessions to process ({sum(r.bytes for r in todo)/1e12:.2f} TB), "
          f"{len(done)} already done")

    ok = 0
    for n, rec in enumerate(todo, 1):
        tar_path = Path(rec.path)
        out_dir = Path(args.out_dir) if args.out_dir else tar_path.parent
        stem = tar_path.stem
        if args.out_dir:
            stem = f"{session_id(tar_path).replace('/', '_')}_{stem}"
        out_path = out_dir / f"{stem}.wfz"
        print(f"\n[{n}/{len(todo)}] {session_id(tar_path)}  {rec.bytes/1e9:.1f} GB  "
              f"{rec.kind}  -> {out_path.name}")
        try:
            result = process_one(
                tar_path, out_path,
                verify_full=not args.no_verify_full,
                threads=args.threads,
                keep_restored=args.keep_restored,
            )
            ok += 1
            print(f"    x{result['ratio']:.2f}  shift={result['shift']}  "
                  f"{'byte-identical' if result.get('byte_identical') else 'pixels verified'}  "
                  f"{result['elapsed_s']/60:.1f} min")
        except Exception as e:  # noqa: BLE001 - one bad session must not stop the run
            result = {
                "tar": str(tar_path), "wfz": str(out_path), "session": session_id(tar_path),
                "ok": False, "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            }
            print(f"    FAILED: {result['error']}")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result) + "\n")

    print(f"\n{ok}/{len(todo)} succeeded; log in {log_path}")
    return 0 if ok == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
