"""Watch sahale's responsiveness from the workstation and stop the ephys job if it degrades.

On 2026-08-13 the ephys campaign took sahale off the network and the overload removed the only way
to stop it: ssh could not complete a login. The lesson is that **the abort path must not depend on
the thing that is failing**. So this runs here, not there, and it aborts by creating a file on the
share over SMB - no shell on sahale required.

What it measures is deliberately close to what the lab actually cares about: how long a small
metadata read takes. A file server that has stopped answering is one where `stat` hangs, and that
shows up long before anything else a person would notice.

    python scripts/sahale_watchdog.py                     # watch and act
    python scripts/sahale_watchdog.py --dry-run           # watch and report only

It writes the stop file after `--strikes` consecutive slow or failed probes, then keeps watching
and reports recovery. It never deletes the stop file - restarting is a human decision.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def probe(path: str, timeout_note: list) -> float | None:
    """Seconds for a small metadata read, or None if it failed."""
    t0 = time.perf_counter()
    try:
        os.stat(path)
    except OSError as e:
        timeout_note.append(str(e)[:80])
        return None
    return time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe-path", default=r"Y:\Subjects",
                    help="something small and always present on the share")
    ap.add_argument("--stop-file", default=r"Y:\temp\ephys_stop",
                    help="created on the share, so sahale's own job sees it without a shell")
    ap.add_argument("--interval-s", type=float, default=30)
    ap.add_argument("--slow-s", type=float, default=5.0,
                    help="a probe slower than this counts as a strike")
    ap.add_argument("--strikes", type=int, default=3,
                    help="consecutive strikes before the stop file is written")
    ap.add_argument("--log", default=r"D:\temp\sahale_watchdog.log")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)

    def note(msg: str) -> None:
        line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {msg}"
        with log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, flush=True)

    note(f"watchdog starting: probing {args.probe_path} every {args.interval_s:.0f}s; "
         f"{args.strikes} consecutive probes over {args.slow_s:.1f}s writes {args.stop_file}"
         + ("  [DRY RUN]" if args.dry_run else ""))

    baseline: list[float] = []
    strikes = 0
    fired = False
    while True:
        errs: list[str] = []
        dt = probe(args.probe_path, errs)
        if dt is None:
            strikes += 1
            note(f"probe FAILED ({errs[0] if errs else '?'})  strike {strikes}/{args.strikes}")
        elif dt > args.slow_s:
            strikes += 1
            note(f"probe slow: {dt:.2f}s  strike {strikes}/{args.strikes}")
        else:
            if strikes:
                note(f"probe recovered: {dt:.3f}s  (strikes reset)")
            strikes = 0
            baseline.append(dt)
            if len(baseline) % 60 == 0:
                note(f"healthy: median probe {statistics.median(baseline[-60:])*1000:.0f} ms "
                     f"over the last {min(60, len(baseline))} probes")

        if strikes >= args.strikes and not fired:
            if args.dry_run:
                note("WOULD WRITE THE STOP FILE (dry run); continuing to watch")
                fired = True
            else:
                try:
                    Path(args.stop_file).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.stop_file).touch()
                    note(f"*** WROTE {args.stop_file} - the ephys job will stop cleanly ***")
                    fired = True
                except OSError as e:
                    # If the share is unreachable we cannot write to it, which is itself the
                    # worst case. Say so loudly rather than failing silently.
                    note(f"*** COULD NOT WRITE THE STOP FILE: {e} - the share may already be "
                         f"unreachable. Stop the job by hand. ***")
        time.sleep(args.interval_s)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nwatchdog stopped by hand")
        sys.exit(0)
