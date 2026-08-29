"""Bulk mtscomp driver. Never deletes anything.

**Where to run it, updated 2026-08-28.** This was written to run on sahale, because a second reader
over SMB got ~20 MB/s - but that was measured while the widefield campaign was saturating the
share. With the pool quiet, SMB from the workstation reads at **201 MB/s**, so the job can run
here instead. That costs roughly double the wall clock and buys a failure mode that cannot take the
lab's file server offline, which is what happened on 2026-08-13. See `docs/EPHYS_RESTART_PLAN.md`.

    # from the workstation, over SMB, the recommended way
    python scripts/ephys_compress.py --root \\\\sahale.biostr.washington.edu\\data\\Subjects \\
        --procs 2 --threads 4 --smallest-first --max-tb 0.5 \\
        --stop-file D:\\temp\\ephys_stop


Standalone on purpose. `wfcompress.lab.batch` cannot be reused here: it annotates with `X | None`
and its package `__init__` imports `imagecodecs`, which has no FreeBSD wheels. This needs only
numpy (already on the box at 1.22.4), plus `mtscomp.py` and `tqdm` staged beside it, and runs on
Python 3.9 with no pip.

    python3.9 ephys_compress.py --root /mnt/data/data/Subjects --dry-run
    python3.9 ephys_compress.py --root /mnt/data/data/Subjects --procs 8 --threads 4

Design follows what the widefield campaign learned the hard way:

* **Processes, not threads.** Measured on this box: 8 processes x 4 threads gives 139 MB/s where
  one process at 32 threads gives 47. mtscomp's verify pass is serial, so it only overlaps across
  processes.
* **Atomic output.** Each worker writes ``.cbin.partial-<pid>`` and ``.ch.partial-<pid>`` and
  renames on success. Two widefield crashes left 297 GB and 163 GB of half-written output; only
  the naming convention made it safe to reclaim. The ``.ch`` embeds no filenames, so renaming is
  sound.
* **Resume re-checks the artifact**, not just the log line: a recorded success whose ``.cbin`` is
  missing or the wrong size is redone.
* **An append-only file log** of everything created or replaced, for auditing later.
* **No delete path.** Removing a raw ``.bin`` is a separate, gated decision made against
  ``sha1_uncompressed``, which mtscomp records in the ``.ch`` and which lets the original be
  verified from the compressed copy alone.
"""

# ruff: noqa: UP031
# Percent formatting is kept in the multi-line report strings below. This file is copied to a
# FreeBSD appliance and run by an interpreter this repo's tooling never sees, so it is deliberately
# plain; the remaining sites are wrapped across lines where %-style reads better than an f-string.

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import os.path as op
import platform
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone

sys.path.insert(0, op.dirname(op.abspath(__file__)))

RAW_SUFFIXES = (".ap.bin", ".lf.bin", ".nidq.bin")
FILE_LOG_FIELDS = ["timestamp_utc", "event", "path", "size_bytes", "host", "pid", "note"]


# --------------------------------------------------------------------------------------------
# discovery


def read_meta(path):
    """(n_channels, sample_rate) from a SpikeGLX .meta, or (0, 0.0)."""
    n, rate = 0, 0.0
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if line.startswith("nSavedChans="):
                    n = int(line.split("=", 1)[1])
                elif re.match(r"^(imSampRate|niSampRate)=", line):
                    rate = float(line.split("=", 1)[1])
    except OSError:
        return 0, 0.0
    return n, rate


def discover(root, errors):
    """Every raw .bin under root, with the metadata needed to compress it.

    Walks with os.scandir rather than listdir plus stat: the entry type comes back with the
    directory listing, which on the census over SMB was the difference between 283 s and 5 s. It
    matters less locally but costs nothing.
    """
    out, stack = [], [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError as e:
            errors.append(f"{d}: {e}")
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.name.endswith(RAW_SUFFIXES):
                    out.append(e.path)
            except OSError as ex:
                errors.append(f"{e.path}: {ex}")
    return sorted(out)


def describe(path):
    stem = path[: -len(".bin")]
    rec = {
        "bin": path, "cbin": stem + ".cbin", "ch": stem + ".ch", "meta": stem + ".meta",
        "bytes": 0, "n_channels": 0, "sample_rate": 0.0, "skip": "",
    }
    try:
        rec["bytes"] = op.getsize(path)
    except OSError as e:
        rec["skip"] = f"cannot stat: {e}"
        return rec
    if op.isfile(rec["cbin"]) and op.isfile(rec["ch"]):
        rec["skip"] = "already has a .cbin and .ch"
        return rec
    if not op.isfile(rec["meta"]):
        rec["skip"] = "no .meta, so channel count and sample rate are unknown"
        return rec
    rec["n_channels"], rec["sample_rate"] = read_meta(rec["meta"])
    if not rec["n_channels"] or not rec["sample_rate"]:
        rec["skip"] = "could not parse nSavedChans / sample rate from the .meta"
    elif rec["bytes"] % (2 * rec["n_channels"]):
        rec["skip"] = ("size %d is not a whole number of %d-channel int16 samples; the file or "
                       "the .meta is wrong" % (rec["bytes"], rec["n_channels"]))
    elif rec["bytes"] == 0:
        rec["skip"] = "empty file"
    return rec


# --------------------------------------------------------------------------------------------
# logging


def file_log(path, event, target, size=None, note=""):
    """Append one row. Never raises - an audit failure must not abort real work."""
    if not path:
        return
    try:
        if size is None:
            size = op.getsize(target) if op.exists(target) else ""
        row = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event, "path": target, "size_bytes": size,
            "host": platform.node(), "pid": os.getpid(), "note": note,
        }
        new = not op.exists(path) or op.getsize(path) == 0
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=FILE_LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
        with open(path, "a", newline="") as fh:
            fh.write(buf.getvalue())
    except Exception:  # noqa: BLE001 - logging must never be the thing that fails a run
        pass


# --------------------------------------------------------------------------------------------
# the work


class _NoBar:
    """tqdm stand-in. Eight workers each drawing a progress bar is unreadable, and mtscomp's
    verify pass does not honour its own `quiet` flag."""

    def __init__(self, iterable=None, *a, **k):
        self._it = iterable if iterable is not None else []

    def __iter__(self):
        return iter(self._it)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, *a, **k):
        pass

    def close(self):
        pass


def process_one(job):
    """Compress one file. Runs in a child process; returns a plain dict."""
    rec, threads, file_log_path, stop_file = job
    t0 = time.time()
    out = {"bin": rec["bin"], "cbin": rec["cbin"], "bytes": rec["bytes"],
           "n_channels": rec["n_channels"], "sample_rate": rec["sample_rate"], "ok": False}

    # Checked here as well as in the dispatch loop. On 2026-08-13 this job took the file server off
    # the network and the overload removed the only way to stop it - ssh could not complete a
    # login. A stop file on the share can be created over SMB with no shell at all. Checking it in
    # the worker means a queued file is abandoned before it starts, so a stop takes effect within
    # one file per worker rather than waiting for the whole pool to drain.
    if stop_file and op.exists(stop_file):
        out["skipped"] = "stop file present"
        return out

    import mtscomp
    mtscomp.tqdm = _NoBar

    pid = os.getpid()
    tmp_cbin = "%s.partial-%d" % (rec["cbin"], pid)
    tmp_ch = "%s.partial-%d" % (rec["ch"], pid)
    try:
        for p in (tmp_cbin, tmp_ch):
            if op.exists(p):
                os.remove(p)
        file_log(file_log_path, "create", tmp_cbin, 0, "temporary output")
        # check_after_compress decompresses and compares against the original before returning;
        # a failure raises and nothing is renamed into place.
        mtscomp.compress(
            rec["bin"], tmp_cbin, tmp_ch,
            sample_rate=rec["sample_rate"], n_channels=rec["n_channels"], dtype="int16",
            n_threads=threads, check_after_compress=True, quiet=True,
        )
        csize = op.getsize(tmp_cbin) + op.getsize(tmp_ch)
        with open(tmp_ch) as fh:
            cmeta = json.load(fh)

        file_log(file_log_path, "delete", tmp_cbin, csize, "renamed into place")
        os.replace(tmp_cbin, rec["cbin"])
        os.replace(tmp_ch, rec["ch"])
        file_log(file_log_path, "create", rec["cbin"])
        file_log(file_log_path, "create", rec["ch"])

        out.update(
            ok=True, cbin_bytes=op.getsize(rec["cbin"]), ch_bytes=op.getsize(rec["ch"]),
            ratio=rec["bytes"] / float(csize),
            sha1_uncompressed=cmeta.get("sha1_uncompressed", ""),
            sha1_compressed=cmeta.get("sha1_compressed", ""),
            mtscomp_version=cmeta.get("version", ""),
            verified="check_after_compress",
        )
    except BaseException as e:  # noqa: BLE001 - one bad file must not stop the run
        for p in (tmp_cbin, tmp_ch):
            if op.exists(p):
                file_log(file_log_path, "delete", p, note="discarded, compression failed")
                try:
                    os.remove(p)
                except OSError:
                    pass
        out["error"] = f"{type(e).__name__}: {e}"
    out["elapsed_s"] = time.time() - t0
    return out


def already_done(rec, log_path):
    """Successes worth trusting: the .cbin must still be there at the recorded size."""
    done = {}
    if not op.isfile(log_path):
        return done
    with open(log_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not r.get("ok"):
                continue
            cbin = r.get("cbin", "")
            try:
                if op.getsize(cbin) == r.get("cbin_bytes"):
                    done[r["bin"]] = r
            except OSError:
                pass
    return done


def clean_partials(paths, file_log_path):
    """Remove leftover *.partial-* beside the files we are about to work on."""
    n = total = 0
    for d in sorted({op.dirname(p) for p in paths}):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if ".partial-" in name and (".cbin" in name or ".ch" in name):
                p = op.join(d, name)
                try:
                    size = op.getsize(p)
                    file_log(file_log_path, "delete", p, size, "stale temporary")
                    os.remove(p)
                    n += 1
                    total += size
                except OSError:
                    pass
    return n, total


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, help="e.g. /mnt/data/data/Subjects")
    ap.add_argument("--procs", type=int, default=8)
    ap.add_argument("--threads", type=int, default=4, help="threads inside each process")
    ap.add_argument("--log", default=op.expanduser("~/ephys_run.jsonl"))
    ap.add_argument("--file-log", default=op.expanduser("~/ephys_files.csv"))
    ap.add_argument("--min-age-s", type=float, default=3600,
                    help="skip files modified more recently; data arrives continuously")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--max-tb", type=float, help="stop after this much source has been done")
    ap.add_argument("--smallest-first", action="store_true")
    ap.add_argument("--shard", default="",
                    help="i/n - take only files whose stable hash of the path relative to --root "
                         "falls in shard i of n. Lets several machines share the corpus with no "
                         "coordination beyond agreeing on n.")
    ap.add_argument("--below-normal", action="store_true",
                    help="run at below-normal priority so the machine stays usable; workers "
                         "inherit it from the parent. Windows only, a no-op elsewhere.")
    ap.add_argument("--stop-file", default="",
                    help="path checked before each file and after each completion; create it to "
                         "stop cleanly. Put it somewhere reachable without a shell - on the share "
                         "- so an overloaded server can still be stopped.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-partials", action="store_true")
    args = ap.parse_args()

    print(f"discovering under {args.root} ...", flush=True)
    errors = []
    t0 = time.time()
    paths = discover(args.root, errors)
    print("  %d raw .bin found in %.0f s; %d unreadable directories"
          % (len(paths), time.time() - t0, len(errors)), flush=True)
    for e in errors[:5]:
        print(f"    {e}")

    recs = [describe(p) for p in paths]
    now = time.time()
    todo, skipped = [], []
    for r in recs:
        if r["skip"]:
            skipped.append(r)
        elif now - op.getmtime(r["bin"]) < args.min_age_s:
            r["skip"] = "modified less than %.0f min ago" % (args.min_age_s / 60)
            skipped.append(r)
        else:
            todo.append(r)

    done = already_done(recs, args.log)
    todo = [r for r in todo if r["bin"] not in done]

    # Sharding, so several machines can work the same corpus without colliding. The partition is a
    # stable hash of the path *relative to --root*, never the absolute path: this workstation sees
    # Y:/Subjects/... and sahale sees /mnt/data/data/Subjects/..., so hashing the absolute form
    # would put the same file in different shards on different machines and they would duplicate
    # each other's work. md5 rather than hash(), which Python randomises per process.
    if args.shard:
        try:
            i_s, n_s = args.shard.split("/")
            i_s, n_s = int(i_s), int(n_s)
        except ValueError:
            print("--shard must look like i/n, e.g. 0/2")
            return 2
        if not (0 <= i_s < n_s):
            print("--shard i must be in 0..n-1")
            return 2

        def _shard_of(p):
            rel = op.relpath(p, args.root).replace("\\", "/").lower()
            return int(hashlib.md5(rel.encode("utf-8")).hexdigest(), 16) % n_s

        before = len(todo)
        todo = [r for r in todo if _shard_of(r["bin"]) == i_s]
        print("\nshard %d of %d: %d of %d remaining files are mine"
              % (i_s, n_s, len(todo), before))

    todo.sort(key=lambda r: r["bytes"] if args.smallest_first else -r["bytes"])
    if args.limit:
        todo = todo[: args.limit]

    tb = sum(r["bytes"] for r in todo) / 1e12
    print("\n%d already compressed and verified (from %s)" % (len(done), args.log))
    print("%d to skip:" % len(skipped))
    reasons = {}
    for r in skipped:
        key = r["skip"].split(";")[0][:60]
        reasons[key] = reasons.get(key, 0) + 1
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("   %5d  %s" % (v, k))
    print("%d to compress, %.2f TB" % (len(todo), tb))
    if not todo:
        print("nothing to do")
        return 0
    print("expect roughly %.1f days at 139 MB/s" % (tb * 1e12 / 139e6 / 86400))

    if args.dry_run:
        print("\n--dry-run: nothing written. Largest few:")
        for r in todo[:5]:
            print("   {:8.1f} GB  {}".format(r["bytes"] / 1e9, r["bin"]))
        return 0

    if not args.keep_partials:
        n, nb = clean_partials([r["bin"] for r in todo], args.file_log)
        if n:
            print("removed %d stale temporary file(s), %.1f GB reclaimed" % (n, nb / 1e9))

    if args.stop_file and op.exists(args.stop_file):
        print("\nstop file %s is present; not starting. Remove it first."
              % args.stop_file)
        return 0

    # Below-normal priority, set on the parent so the pool's workers inherit it. The widefield
    # campaign held 13 of 16 cores and made the workstation unpleasant to use; this costs nothing
    # while the machine is idle but lets interactive work preempt. No-op off Windows.
    if args.below_normal:
        try:
            import ctypes
            BELOW_NORMAL = 0x00004000
            k = ctypes.windll.kernel32
            # argtypes matter here. Without them ctypes marshals the pseudo-handle (HANDLE)-1 as a
            # 32-bit int, SetPriorityClass rejects it, and the call silently fails - which is
            # exactly what happened on the first attempt.
            k.GetCurrentProcess.restype = ctypes.c_void_p
            k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            k.GetPriorityClass.argtypes = [ctypes.c_void_p]
            h = k.GetCurrentProcess()
            if k.SetPriorityClass(h, BELOW_NORMAL) and k.GetPriorityClass(h) == BELOW_NORMAL:
                print("priority: below normal (workers inherit)")
            else:
                print("priority: could not lower it; continuing at normal")
        except (AttributeError, OSError) as e:
            print("priority: not adjusted (%s)" % e)
    if args.stop_file:
        print("stop cleanly at any time by creating: %s" % args.stop_file)
    print("\nrunning %d processes x %d threads\n" % (args.procs, args.threads), flush=True)
    t_start, ok, done_bytes = time.time(), 0, 0
    jobs = [(r, args.threads, args.file_log, args.stop_file) for r in todo]
    with ProcessPoolExecutor(args.procs) as pool:
        futures = {pool.submit(process_one, j): j[0] for j in jobs}
        for i, fut in enumerate(as_completed(futures), 1):
            rec = futures[fut]
            try:
                res = fut.result()
            except BaseException as e:  # noqa: BLE001
                res = {"bin": rec["bin"], "ok": False,
                       "error": f"{type(e).__name__}: {e}"}
            with open(args.log, "a") as fh:
                fh.write(json.dumps(res) + "\n")
            if res.get("ok"):
                ok += 1
                done_bytes += res.get("bytes", 0)
                rate = done_bytes / 1e6 / (time.time() - t_start)
                print("[%d/%d] %s  x%.2f  %.1f min  (%.0f MB/s aggregate)"
                      % (i, len(jobs), op.basename(op.dirname(res["bin"])),
                         res.get("ratio", 0), res.get("elapsed_s", 0) / 60, rate), flush=True)
            else:
                print("[%d/%d] FAILED %s\n    %s"
                      % (i, len(jobs), res["bin"], res.get("error", "?")), flush=True)
            if res.get("skipped"):
                print("[%d/%d] skipped %s (%s)"
                      % (i, len(jobs), op.basename(op.dirname(res["bin"])), res["skipped"]),
                      flush=True)
            if args.stop_file and op.exists(args.stop_file):
                print(f"\nstop file {args.stop_file} present; cancelling queued work. "
                      f"Files already running will finish. Rerun to continue.")
                for f in futures:
                    f.cancel()
                break
            if args.max_tb and done_bytes / 1e12 >= args.max_tb:
                print(f"\nreached --max-tb {args.max_tb:.2f}; stopping. Rerun to continue.")
                for f in futures:
                    f.cancel()
                break

    el = time.time() - t_start
    print("\n%d/%d succeeded in %.2f h (%.0f MB/s aggregate); log in %s"
          % (ok, len(jobs), el / 3600, done_bytes / 1e6 / max(el, 1), args.log))
    print("Nothing was deleted. This script has no delete path.")
    return 0 if ok == len(jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
