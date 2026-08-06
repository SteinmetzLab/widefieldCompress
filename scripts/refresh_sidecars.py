"""Rewrite the `.wfz.README.md` beside every finished archive.

The README next to each .wfz tells whoever finds it how to get the data back. When those
instructions change - as they did when `wfcompress extract` was added - the files already on the
share still carry the old text, and the running bulk job keeps writing the old text because it
imported the code before the change.

Regenerating is cheap: it reads the .wfz footer only, not the payload, so it is roughly a second
per archive rather than the hours compression took. Nothing else is touched.

Dry run by default. Pass --write to actually replace the files.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from wfcompress import filelog, read_meta, sidecar

HERE = Path(__file__).resolve().parents[1]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--log", default=str(HERE / "data" / "bulk.jsonl"))
    p.add_argument("--file-log", default=str(HERE / "data" / "fileEditLog.csv"))
    p.add_argument("--write", action="store_true", help="actually rewrite the sidecars")
    p.add_argument("--receipts", action="store_true", help="rewrite receipt.json too")
    args = p.parse_args()

    rows = [json.loads(ln) for ln in Path(args.log).read_text().splitlines() if ln.strip()]
    done = [r for r in rows if r.get("ok")]
    print(f"{len(done)} finished archives in {args.log}")
    if args.write:
        filelog.ensure(args.file_log)

    n_ok = n_missing = n_failed = 0
    for r in done:
        wfz = Path(r["wfz"])
        if not wfz.exists():
            n_missing += 1
            continue
        try:
            meta = read_meta(wfz)
            # the receipt records the campaign's own verification result, which is not in the
            # footer; carry it across so the regenerated README says "verified" where it should
            meta.setdefault("output_bytes", r["output_bytes"])
            meta.setdefault("ratio", r["ratio"])
            meta["byte_identical_verified"] = bool(r.get("byte_identical"))
            if args.write:
                sidecar.write_readme(wfz, meta, file_log=args.file_log)
                if args.receipts:
                    sidecar.write_receipt(wfz, meta, file_log=args.file_log)
            n_ok += 1
        except Exception as e:  # noqa: BLE001 - report every failure, keep going
            n_failed += 1
            print(f"  FAILED {wfz}: {type(e).__name__}: {e}")

    verb = "rewrote" if args.write else "would rewrite"
    print(f"{verb} {n_ok} READMEs" + ("  (+receipts)" if args.receipts and args.write else ""))
    if n_missing:
        print(f"{n_missing} .wfz listed as done but not on disk")
    if n_failed:
        print(f"{n_failed} failed")
    if not args.write:
        print("\ndry run - pass --write to apply")
    return 1 if n_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
