"""Keep the bulk compression run alive, and record every time it stops.

The batch driver has now died silently three times - clean stderr, no traceback, no reboot, the
parent simply gone. Each occurrence costs hours: the eight in-flight archives are lost and nobody
notices until someone looks. The driver is already built to be restarted (it skips completed
sessions and reclaims stale partials), so the missing piece is something to do the restarting.

This launches it, waits, logs the exit, and relaunches. It stops when the driver reports nothing
left to do, or when the stop file appears.

    pythonw supervise_bulk.py            # detached, the normal way to run it
    python  supervise_bulk.py --once     # one launch, for checking the wiring

Stop it by creating the stop file (default D:/temp/wfc_stop) - the current run is left to finish
rather than killed, so nothing is lost.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
DEFAULT_ARGS = [
    "-u", "-m", "wfcompress.lab.batch",
    "--census", "data/census_Y.csv", "--server", "Y",
    "--jobs", "8", "--threads", "4", "--largest-first",
    "--assume-shape", "560", "560",
    "--log", "data/bulk.jsonl", "--file-log", "data/fileEditLog.csv",
]


def note(log: Path, msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {msg}"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--python", default=str(Path(sys.executable).with_name("python.exe")))
    ap.add_argument("--log", default=r"D:\temp\wfc_supervisor.log")
    ap.add_argument("--out", default=r"D:\temp\wfc_run.out")
    ap.add_argument("--stop-file", default=r"D:\temp\wfc_stop")
    ap.add_argument("--gap-s", type=float, default=60, help="pause between relaunches")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    log, stop = Path(args.log), Path(args.stop_file)
    note(log, f"supervisor starting; stop by creating {stop}")

    attempt = 0
    while True:
        if stop.exists():
            note(log, "stop file present; not launching")
            return 0
        attempt += 1
        out_path = Path(args.out).with_name(f"{Path(args.out).stem}_{attempt:03d}.out")
        err_path = out_path.with_suffix(".err")
        note(log, f"launch #{attempt} -> {out_path.name}")
        t0 = time.time()
        with out_path.open("w", encoding="utf-8") as fo, \
                err_path.open("w", encoding="utf-8") as fe:
            rc = subprocess.call([args.python, *DEFAULT_ARGS], cwd=str(HERE),
                                 stdout=fo, stderr=fe)
        dt = time.time() - t0

        text = out_path.read_text(encoding="utf-8", errors="replace")
        errtext = err_path.read_text(encoding="utf-8", errors="replace").strip()
        done = re.search(r"^(\d+) sessions to process", text, re.M)
        remaining = int(done.group(1)) if done else -1
        note(log, f"  exit {rc} after {dt/3600:.2f} h; driver reported {remaining} to process"
                  + (f"; stderr: {errtext.splitlines()[-1][:120]}" if errtext else
                     "; stderr empty"))

        if remaining == 0:
            note(log, "nothing left to process - campaign complete, supervisor exiting")
            return 0
        if args.once:
            note(log, "--once given, exiting")
            return rc
        if dt < 120:
            # A run that dies in under two minutes is failing to start, not failing partway.
            # Relaunching in a tight loop would bury the real error under thousands of restarts.
            note(log, "  run lasted under 2 min; backing off 10 min")
            time.sleep(600)
        else:
            time.sleep(args.gap_s)


if __name__ == "__main__":
    raise SystemExit(main())
