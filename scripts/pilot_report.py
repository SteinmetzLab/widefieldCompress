"""Summarise the staged pilot and project the full Y: run from it."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from wfcompress import filelog

HERE = Path(__file__).resolve().parents[1]
DATA = HERE / "data"

rows = []
for name in ("pilot_stage_a.jsonl", "pilot_stage_b.jsonl"):
    p = DATA / name
    if p.exists():
        rows += [json.loads(x) for x in p.read_text().splitlines()]

ok = [r for r in rows if r.get("ok")]
bad = [r for r in rows if not r.get("ok")]
tin = sum(r["source_bytes"] for r in ok)
tout = sum(r["output_bytes"] for r in ok)

print("=" * 74)
print(f"STAGED PILOT: {len(ok)}/{len(rows)} succeeded")
print(f"  {tin/1e9:.1f} GB -> {tout/1e9:.1f} GB   pooled x{tin/tout:.2f}   "
      f"{100*(1-tout/tin):.1f}% saved")
bi = sum(bool(r.get("byte_identical")) for r in ok)
print(f"  byte-identical: {bi}/{len(ok)}")
assumed = [r for r in ok if r.get("shape_assumed")]
print(f"  geometry supplied by --assume-shape: {len(assumed)}")
for r in bad:
    print(f"  FAILED {r['session']}: {r['error'][:90]}")

print(f"\n  ratio range: {min(r['ratio'] for r in ok):.2f} - {max(r['ratio'] for r in ok):.2f}")
print(f"  largest session: {max(r['source_bytes'] for r in ok)/1e9:.1f} GB at "
      f"x{max(ok, key=lambda r: r['source_bytes'])['ratio']:.2f}")

# --- what the corpus costs ------------------------------------------------------------------
census = list(csv.DictReader((DATA / "census_Y.csv").open(encoding="utf-8")))
wf = [r for r in census if r["kind"] in ("frame-N", "basler-tiff") and int(r["bytes"]) > 0]
corpus = sum(int(r["bytes"]) for r in wf)

print("\n" + "=" * 74)
print(f"FULL Y: RUN  ({len(wf)} archives, {corpus/1e12:.2f} TB)")

RATES = [
    ("full queue, 8 jobs x 4 threads (sweep, 8 equal sessions)", 86.7e6),
    ("staged pilot stage A (12 uneven sessions, tail-limited)", 65.1e6),
    ("single session, 16 threads (stage B, the 430 GB archive)", 34.1e6),
]
print(f"\n  {'measured arrangement':<58s}{'MB/s':>7s}{'days':>7s}")
for label, rate in RATES:
    print(f"  {label:<58s}{rate/1e6:7.1f}{corpus/rate/86400:7.1f}")

print("\n  Stage A is the pessimistic bound and stage B the floor, not the expectation:")
print("  with only 12 items and sizes from 1 to 32 GB, the four biggest ran alone for most of")
print("  the window, so fewer than 8 workers were busy. A 1,120-session queue stays full.")
print("  Size itself does not cost throughput: the 430 GB archive ran at 34.1 MB/s on 16")
print("  threads against 37.8 MB/s measured for a 3 GB one at the same thread count.")

ratio = tin / tout
retained = corpus / ratio
print(f"\n  At the pooled pilot ratio of x{ratio:.2f}:")
print(f"    {corpus/1e12:.1f} TB -> {retained/1e12:.1f} TB retained, "
      f"{(corpus-retained)/1e12:.1f} TB reclaimed")
print(f"  Backblaze at ~$6/TB/month: ${(corpus-retained)/1e12*6*12:,.0f}/year once versions expire")

# --- what the audit log says ----------------------------------------------------------------
log = DATA / "fileEditLog.csv"
if log.exists():
    print("\n" + "=" * 74)
    print(f"FILE EDIT LOG ({log})")
    for event, v in sorted(filelog.summarise(log).items()):
        size = "" if event == "transient" else f"  {v['bytes']/1e9:8.2f} GB"
        print(f"  {event:10s} n={v['n']:4d}{size}")
    persistent_deletes = [
        r for r in csv.DictReader(log.open(encoding="utf-8"))
        if r["event"] == "delete" and not filelog.is_transient(r["path"])
    ]
    print(f"\n  persistent files deleted: {len(persistent_deletes)}")
    assert not persistent_deletes, "something was deleted that should not have been"
