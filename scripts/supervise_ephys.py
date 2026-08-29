"""Keep the ephys compression alive, and record every time it stops.

The widefield campaign needed this and so does this one. Stage 3 was killed part way through with
no error, no memory pressure, nothing in the Windows event log and sahale healthy - the run simply
ended, most likely against a background-task lifetime limit, after stages 1 and 2 had completed at
6.2 and 7.7 hours. A job measured in weeks cannot depend on a process nobody restarts.

The driver is already built to be restarted: it re-derives what is done from its own log, re-checks
that each recorded `.cbin` still exists at the right size, and reclaims stale `*.partial-*` on the
way in. So the missing piece is only the restarting.

    pythonw scripts/supervise_ephys.py        # detached, the normal way to run it
    python  scripts/supervise_ephys.py --once # one launch, for checking the wiring

Stop it by creating the stop file, which is the same one the driver checks - the current run is
left to finish its in-flight files rather than killed, so nothing is thrown away.
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
    "-u", str(HERE / "scripts" / "ephys_compress.py"),
    "--root", "Y:/Subjects",
    "--log", r"D:\temp\ephys_run.jsonl",
    "--file-log", str(HERE / "data" / "ephysFileLog.csv"),
    "--stop-file", r"D:\temp\ephys_stop",
    "--smallest-first", "--below-normal",
]


def note(log: Path, msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {msg}"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # pythonw, not python: python.exe allocates a console, and closing that window sends a control
    # event that kills the child. That cost the widefield campaign 35 hours of work once.
    ap.add_argument("--python", default=str(Path(sys.executable).with_name("pythonw.exe")))
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--log", default=r"D:\temp\ephys_supervisor.log")
    ap.add_argument("--out", default=r"D:\temp\ephys_run.out")
    ap.add_argument("--stop-file", default=r"D:\temp\ephys_stop")
    ap.add_argument("--gap-s", type=float, default=60)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    log, stop = Path(args.log), Path(args.stop_file)
    note(log, f"supervisor starting; {args.procs} procs x {args.threads} threads; "
              f"stop by creating {stop}")

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
            rc = subprocess.call(
                [args.python, *DEFAULT_ARGS,
                 "--procs", str(args.procs), "--threads", str(args.threads)],
                cwd=str(HERE), stdout=fo, stderr=fe)
        dt = time.time() - t0

        text = out_path.read_text(encoding="utf-8", errors="replace")
        errtext = err_path.read_text(encoding="utf-8", errors="replace").strip()
        m = re.search(r"^(\d+) to compress", text, re.M)
        remaining = int(m.group(1)) if m else -1
        done = re.findall(r"^\[(\d+)/\d+\]", text, re.M)
        note(log, f"  exit {rc} after {dt/3600:.2f} h; driver reported {remaining} to compress; "
                  f"{len(done)} completions this run"
                  + (f"; stderr: {errtext.splitlines()[-1][:120]}" if errtext else
                     "; stderr empty"))

        if remaining == 0:
            note(log, "nothing left to compress - campaign complete, supervisor exiting")
            return 0
        if args.once:
            note(log, "--once given, exiting")
            return rc
        if dt < 120:
            # A run that dies inside two minutes is failing to start, not failing partway.
            # Relaunching tightly would bury the real error under thousands of restarts.
            note(log, "  run lasted under 2 min; backing off 10 min")
            time.sleep(600)
        else:
            time.sleep(args.gap_s)


if __name__ == "__main__":
    raise SystemExit(main())
