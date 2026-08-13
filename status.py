"""One command for the state of everything. Run from the repo root:

    D:\\temp\\wfc-venv\\Scripts\\python.exe status.py

Reports both compression campaigns and the offsite backup. Read-only; starts nothing, stops
nothing. Add --b2 to also query Backblaze (slower, needs the read-only key).
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SSH_KEY = Path.home() / ".ssh" / "sahale_wfc"
SAHALE = r"NETID\nsteinme@sahale.biostr.washington.edu"


def age(path: Path) -> str:
    try:
        mins = (datetime.now().timestamp() - path.stat().st_mtime) / 60
    except OSError:
        return "missing"
    return f"{mins:.0f} min ago" if mins < 180 else f"{mins/60:.1f} h ago"


def widefield() -> None:
    print("=" * 72)
    print("WIDEFIELD  (this workstation, supervised)")
    log = HERE / "data" / "bulk.jsonl"
    if not log.exists():
        print("  no log at", log)
        return
    ok = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    ok = [r for r in ok if r.get("ok")]
    src = sum(r["source_bytes"] for r in ok)
    out = sum(r["output_bytes"] for r in ok)
    bi = sum(1 for r in ok if r.get("byte_identical"))
    print(f"  {len(ok)} of 1,120 archives   {src/1e12:.2f} TB -> {out/1e12:.2f} TB   "
          f"x{src/max(out,1):.2f}")
    print(f"  byte-identical: {bi}/{len(ok)}" + ("" if bi == len(ok) else "   <-- INVESTIGATE"))
    print(f"  last completion: {age(log)}")

    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
             "Where-Object { $_.CommandLine -like '*supervise_bulk*' -or "
             "$_.CommandLine -like '*lab.batch*' } | Measure-Object).Count"],
            capture_output=True, text=True, timeout=60)
        n = ps.stdout.strip()
        print(f"  supervisor + driver processes alive: {n}"
              + ("   <-- NOT RUNNING, see docs/HANDOFF.md section 3" if n == "0" else ""))
    except Exception as e:  # noqa: BLE001
        print(f"  (could not check processes: {e})")
    sup = Path(r"D:\temp\wfc_supervisor.log")
    if sup.exists():
        for line in sup.read_text().splitlines()[-2:]:
            print(f"    {line}")


def ephys() -> None:
    print("=" * 72)
    print("EPHYS  (sahale, over ssh)")
    if not SSH_KEY.exists():
        print("  no ssh key at", SSH_KEY)
        return
    cmd = ("pgrep -f ephys_compress | wc -l; "
           "wc -l < ~/ephys_run.jsonl 2>/dev/null || echo 0; "
           "tail -2 ~/ephys_run.log 2>/dev/null")
    try:
        p = subprocess.run(["ssh", "-i", str(SSH_KEY), "-o", "BatchMode=yes",
                            "-o", "ConnectTimeout=25", SAHALE, cmd],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"  ssh failed: {e}")
        return
    if p.returncode not in (0, 1):
        print(f"  ssh returned {p.returncode}: {p.stderr.strip()[:200]}")
        return
    lines = p.stdout.splitlines()
    if lines:
        n = lines[0].strip()
        print(f"  processes alive: {n}" + ("   <-- NOT RUNNING" if n in ("0", "") else ""))
    if len(lines) > 1:
        print(f"  files finished (log lines): {lines[1].strip()} of 1,900")
    for line in lines[2:]:
        print(f"    {line}")


def b2() -> None:
    print("=" * 72)
    print("BACKBLAZE  (latest snapshot on disk)")
    snaps = sorted((HERE / "data" / "b2_snapshots").glob("b2_*.csv"))
    if not snaps:
        print("  none yet - run scripts/snapshot_b2.py --bucket sahalebackup")
        return
    rows = list(csv.DictReader(snaps[-1].open(encoding="utf-8")))
    have = [r for r in rows if r["in_b2"] == "True"]
    hb = sum(int(r["local_bytes"]) for r in have)
    mb = sum(int(r["local_bytes"]) for r in rows) - hb
    print(f"  {snaps[-1].name}: {len(have)}/{len(rows)} .wfz offsite, "
          f"{hb/1e12:.2f} TB in / {mb/1e12:.2f} TB out ({100*hb/max(hb+mb,1):.0f}%)")
    print("  refresh with: scripts/snapshot_b2.py --bucket sahalebackup")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-ssh", action="store_true")
    args = ap.parse_args()
    print(f"\n{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n")
    widefield()
    if not args.skip_ssh:
        ephys()
    b2()
    print("=" * 72)
    print("Nothing has been deleted. Neither tool has a delete path.")
    print("Full context: docs/HANDOFF.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
