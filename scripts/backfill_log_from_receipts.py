"""Recover log entries for archives that finished after the batch parent stopped logging.

The batch driver's resume check trusts only `bulk.jsonl`. When the parent process died, its
workers finished the archives they were holding, wrote the `.wfz` and its sidecars, and exited -
but the parent was no longer there to append the result. Those archives look unprocessed and would
be recompressed from scratch.

They do not need to be. `process_one` writes `<name>.wfz.receipt.json` only *after* the streaming
verification has passed, and the receipt carries `tar_sha256` and `byte_identical`. So the receipt
is the proof; this reconstructs the log line from it.

Refuses anything whose receipt does not claim byte-identity, or whose .wfz size disagrees with the
receipt. Dry run by default.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=str(HERE / "data" / "bulk.jsonl"))
    ap.add_argument("--file-log", default=str(HERE / "data" / "fileEditLog.csv"))
    ap.add_argument("--write", action="store_true", help="append the recovered rows")
    args = ap.parse_args()

    logged = set()
    for ln in Path(args.log).read_text(encoding="utf-8").splitlines():
        if ln.strip():
            r = json.loads(ln)
            if r.get("ok"):
                logged.add(str(Path(r["wfz"])).lower())

    seen, candidates = set(), []
    for row in csv.DictReader(Path(args.file_log).open(encoding="utf-8")):
        p = row["path"]
        if p.endswith(".wfz") and row["event"] in ("create", "modify"):
            if p.lower() not in logged and p.lower() not in seen:
                seen.add(p.lower())
                candidates.append(Path(p))

    print(f"{len(candidates)} .wfz written but not present in {Path(args.log).name}")
    recovered, refused = [], []
    for wfz in candidates:
        receipt = wfz.with_name(wfz.name + ".receipt.json")
        if not wfz.is_file():
            refused.append((wfz, "the .wfz is gone"))
            continue
        if not receipt.is_file():
            refused.append((wfz, "no receipt - verification never completed"))
            continue
        try:
            r = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            refused.append((wfz, f"unreadable receipt: {e}"))
            continue
        if not r.get("byte_identical"):
            refused.append((wfz, "receipt does not claim byte-identity"))
            continue
        size = wfz.stat().st_size
        if r.get("output_bytes") not in (None, size):
            refused.append((wfz, f"size {size:,} != receipt {r['output_bytes']:,}"))
            continue

        tar = wfz.with_name(r.get("source_name", "widefield.tar"))
        parts = wfz.parts
        recovered.append({
            "tar": str(tar),
            "wfz": str(wfz),
            "session": "/".join(parts[-4:-1]),
            "ratio": r.get("ratio"),
            "source_bytes": r.get("source_bytes"),
            "output_bytes": size,
            "shift": r.get("shift"),
            "n_frames": r.get("n_frames"),
            "shape": r.get("shape"),
            "is_tiff": r.get("is_tiff"),
            "tar_sha256": r.get("tar_sha256"),
            "byte_identical": True,
            "verified_by": "stream",
            "elapsed_s": None,
            "ok": True,
            "recovered_from": "receipt.json after the batch parent stopped logging",
        })

    for w, why in refused:
        print(f"  REFUSED {w}\n          {why}")
    for r in recovered:
        print(f"  ok  {r['session']:<26} {r['source_bytes']/1e9:7.1f} GB -> "
              f"{r['output_bytes']/1e9:6.1f} GB  x{r['ratio']:.2f}  byte-identical")

    saved = sum(r["source_bytes"] for r in recovered)
    print(f"\n{len(recovered)} recoverable; skipping them saves recompressing "
          f"{saved/1e12:.2f} TB")
    if args.write and recovered:
        with Path(args.log).open("a", encoding="utf-8") as fh:
            for r in recovered:
                fh.write(json.dumps(r) + "\n")
        print(f"appended {len(recovered)} rows to {args.log}")
    elif not args.write:
        print("dry run - pass --write to append")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
