"""Sample sahale's health against the thresholds agreed before changing anything.

Run from the workstation. Reports load, ARC hit ratio, CPU idle, swap and the SMB probe latency
that the lab would actually notice, then says plainly whether the numbers sit inside the agreed
envelope. The point of fixing thresholds in advance is to avoid judging a risky change by how the
numbers feel afterwards.

    python scripts/sahale_health.py --samples 6 --interval-s 300
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

SSH = ["ssh", "-i", str(Path.home() / ".ssh" / "sahale_wfc"), "-o", "BatchMode=yes",
       "-o", "ConnectTimeout=25", r"NETID\nsteinme@sahale.biostr.washington.edu"]

REMOTE = r"""
uptime | sed 's/.*load averages: //'
top -b -n 1 | grep '^CPU:' | head -1
swapinfo -h | tail -1 | awk '{print $5}'
H1=$(sysctl -n kstat.zfs.misc.arcstats.hits); M1=$(sysctl -n kstat.zfs.misc.arcstats.misses)
sleep 20
H2=$(sysctl -n kstat.zfs.misc.arcstats.hits); M2=$(sysctl -n kstat.zfs.misc.arcstats.misses)
DH=$((H2-H1)); DM=$((M2-M1)); echo "arc $(( DH*100/(DH+DM+1) ))"
echo "procs $(pgrep -f 'python3.9 ephys_compress' | wc -l | tr -d ' ')"
"""
# `pgrep -c` prints nothing and exits 1 on this FreeBSD, so it silently yielded no count at all.
# Piping through `wc -l` always emits a number. The tag matters as much as the fix: an unlabelled
# bare integer was indistinguishable from a missing reading, and `procs` defaulted to 0 - which
# read as "drained" when the job was still running, and led to a second run being started on top
# of the first. A missing measurement must never render as a confident value.

# agreed before the change, not after
LIMITS = {"load15_abort": 25.0, "load15_ok": 20.0,
          "arc_abort": 70.0, "arc_ok": 85.0,
          "probe_abort_ms": 100.0, "probe_ok_ms": 20.0,
          "swap_abort": 80.0, "swap_ok": 60.0}


def probe_ms(path=r"Y:\Subjects", n=5):
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            os.stat(path)
            out.append((time.perf_counter() - t0) * 1000)
        except OSError:
            out.append(float("inf"))
    return statistics.median(out)


def sample():
    p = subprocess.run([*SSH, REMOTE], capture_output=True, text=True, timeout=180)
    lines = [x.strip() for x in p.stdout.splitlines() if x.strip()]
    # every field starts as "not measured" so a parse failure shows as nan rather than a value
    d = {"load15": float("nan"), "idle": float("nan"), "swap": float("nan"),
         "arc": float("nan"), "procs": float("nan")}
    for ln in lines:
        if re.match(r"^[\d.]+,\s*[\d.]+,\s*[\d.]+$", ln):
            d["load15"] = float(ln.split(",")[2])
        elif ln.startswith("CPU:"):
            m = re.search(r"([\d.]+)%\s*idle", ln)
            if m:
                d["idle"] = float(m.group(1))
        elif ln.endswith("%"):
            d["swap"] = float(ln.rstrip("%"))
        elif ln.startswith("arc "):
            d["arc"] = float(ln.split()[1])
        elif ln.startswith("procs "):
            d["procs"] = int(ln.split()[1])
    d["probe_ms"] = probe_ms()
    return d


def verdict(d):
    bad, warn = [], []
    if d["load15"] > LIMITS["load15_abort"]:
        bad.append(f"load15 {d['load15']:.1f} > {LIMITS['load15_abort']}")
    elif d["load15"] > LIMITS["load15_ok"]:
        warn.append(f"load15 {d['load15']:.1f}")
    if d["arc"] < LIMITS["arc_abort"]:
        bad.append(f"ARC hit {d['arc']:.0f}% < {LIMITS['arc_abort']:.0f}%")
    elif d["arc"] < LIMITS["arc_ok"]:
        warn.append(f"ARC hit {d['arc']:.0f}%")
    if d["probe_ms"] > LIMITS["probe_abort_ms"]:
        bad.append(f"probe {d['probe_ms']:.0f} ms > {LIMITS['probe_abort_ms']:.0f}")
    elif d["probe_ms"] > LIMITS["probe_ok_ms"]:
        warn.append(f"probe {d['probe_ms']:.1f} ms")
    if d["swap"] > LIMITS["swap_abort"]:
        bad.append(f"swap {d['swap']:.0f}% > {LIMITS['swap_abort']:.0f}%")
    elif d["swap"] > LIMITS["swap_ok"]:
        warn.append(f"swap {d['swap']:.0f}%")
    return bad, warn


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", type=int, default=6)
    ap.add_argument("--interval-s", type=float, default=300)
    args = ap.parse_args()

    print(f"{'time':9s} {'procs':>5s} {'load15':>7s} {'idle%':>6s} {'ARChit%':>8s} "
          f"{'swap%':>6s} {'probe ms':>9s}  verdict")
    worst = []
    for i in range(args.samples):
        d = sample()
        bad, warn = verdict(d)
        worst.extend(bad)
        tag = "ABORT" if bad else ("watch: " + ", ".join(warn) if warn else "ok")
        print(f"{datetime.now(timezone.utc):%H:%M:%S} {d['procs']:5.0f} {d['load15']:7.2f} "
              f"{d['idle']:6.1f} {d['arc']:8.0f} {d['swap']:6.0f} {d['probe_ms']:9.1f}  {tag}",
              flush=True)
        if bad:
            print("  *** " + "; ".join(bad))
            print("  *** outside the agreed envelope - stop by creating Y:\\temp\\ephys_stop")
            return 1
        if i < args.samples - 1:
            time.sleep(args.interval_s)
    print("\nall samples inside the agreed envelope")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
