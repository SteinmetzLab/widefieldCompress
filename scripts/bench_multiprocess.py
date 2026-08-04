"""Threads inside one session scale badly. Do concurrent session *processes* scale better?

Each session is independent, so the simplest way past the GIL is not to restructure the codec but
to run several sessions at once in separate processes. This measures whether that actually works.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

OUT = Path(r"D:\temp\wfc-bench")
OUT.mkdir(parents=True, exist_ok=True)
PY = sys.executable

SESSIONS = [
    r"Y:\Subjects\test\2026-02-17\1\widefield.tar",
    r"Y:\Subjects\ZYE_0035\2021-07-17\1\widefield.tar",
    r"Y:\Subjects\AL_0033\2025-03-17\1\widefield.tar",
    r"Y:\Subjects\AL_0048\2026-06-11\4\widefield.tar",
]
TOTAL = sum(Path(s).stat().st_size for s in SESSIONS)
print(f"{len(SESSIONS)} sessions, {TOTAL/1e9:.2f} GB total\n")


def cmd(src, dst, threads):
    # headerless archives carry no geometry; the lab layer knows where to find it
    from wfcompress.lab.session import session_frame_shape

    args = [PY, "-m", "wfcompress.cli", "--threads", str(threads), "--quiet",
            "compress", src, str(dst), "--no-sidecar"]
    shape = session_frame_shape(src)
    if shape:
        args += ["--shape", str(shape[0]), str(shape[1])]
    return args


def clean():
    for f in OUT.glob("mp_*"):
        f.unlink()


def run_sequential(threads):
    clean()
    t0 = time.perf_counter()
    for i, s in enumerate(SESSIONS):
        subprocess.run(cmd(s, OUT / f"mp_{i}.wfz", threads), check=True,
                       capture_output=True)
    dt = time.perf_counter() - t0
    clean()
    return dt


def run_concurrent(threads):
    clean()
    t0 = time.perf_counter()
    procs = [subprocess.Popen(cmd(s, OUT / f"mp_{i}.wfz", threads),
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
             for i, s in enumerate(SESSIONS)]
    for p in procs:
        _, err = p.communicate()
        if p.returncode:
            print(err.decode()[-400:])
            raise SystemExit("a worker failed")
    dt = time.perf_counter() - t0
    clean()
    return dt


print(f"{'arrangement':<44s}{'min':>7s}{'MB/s':>9s}")
print("-" * 60)
for threads in (4, 8):
    dt = run_sequential(threads)
    print(f"{'one at a time, ' + str(threads) + ' threads each':<44s}"
          f"{dt/60:>7.2f}{TOTAL/1e6/dt:>9.1f}")
for threads in (2, 4, 8):
    dt = run_concurrent(threads)
    print(f"{'4 concurrent processes, ' + str(threads) + ' threads each':<44s}"
          f"{dt/60:>7.2f}{TOTAL/1e6/dt:>9.1f}")
