"""Gate condition 7: is each .wfz actually in Backblaze before its tar is considered deletable?

The B2 sync runs nightly. An archive compressed today has not reached the offsite copy yet, so
deleting its tar would leave the only copy of that session on one server. This is the check that
prevents it.

Read-only: uses `b2 file info` and nothing else. Requires that someone has already run
`b2 account authorize` in their own shell - see docs/B2_READONLY_SETUP.md. This script never asks
for, reads, or prints a key.

    python scripts/check_b2_presence.py --bucket BUCKETNAME
    python scripts/check_b2_presence.py --bucket BUCKETNAME --prefix Subjects/ --limit 20
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
B2 = Path(sys.executable).with_name("b2.exe")
if not B2.exists():
    B2 = Path("b2")


def b2_json(args: list[str]) -> tuple[dict | None, str]:
    """Run a b2 subcommand expecting JSON on stdout. Returns (parsed, error_text)."""
    try:
        p = subprocess.run([str(B2), *args], capture_output=True, text=True, check=False)
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"
    if p.returncode != 0:
        return None, (p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout) \
            else f"exit {p.returncode}"
    try:
        return json.loads(p.stdout), ""
    except json.JSONDecodeError as e:
        return None, f"unparseable output: {e}"


def server_to_key(path: str, share_root: str, prefix: str) -> str:
    """Map a share path to the object name the sync task would have given it.

    The Cloud Sync Task mirrors the dataset, so the key is the path relative to the sync root with
    forward slashes. ``--prefix`` covers the case where the task was pointed at a parent.
    """
    p = path.replace("\\", "/")
    low, root = p.lower(), share_root.lower().replace("\\", "/")
    rel = p[len(root):].lstrip("/") if low.startswith(root) else p.lstrip("/")
    return prefix.strip("/") + "/" + rel if prefix.strip("/") else rel


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--audit", default=str(HERE / "data" / "deletable_audit.csv"))
    ap.add_argument("--out", default=str(HERE / "data" / "b2_presence.csv"))
    ap.add_argument("--share-root", default=r"\\sahale.biostr.washington.edu\data")
    ap.add_argument("--prefix", default="", help="object-name prefix if the sync root differs")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    who, err = b2_json(["account", "get"])
    if err:
        print(f"not authorized: {err}\n\nRun this yourself, then re-run me:\n"
              f"  {B2} account authorize\nSee docs/B2_READONLY_SETUP.md", file=sys.stderr)
        return 2
    caps = (who or {}).get("allowed", {}).get("capabilities", [])
    print(f"authorized; capabilities: {', '.join(sorted(caps)) or 'unknown'}")
    for bad in ("deleteFiles", "writeFiles", "deleteBuckets"):
        if bad in caps:
            print(f"  NOTE: this key can {bad}. A read-only key would be preferable.")

    rows = [r for r in csv.DictReader(Path(args.audit).open(encoding="utf-8"))
            if r["verdict"] == "SAFE"]
    if args.limit:
        rows = rows[: args.limit]
    print(f"checking {len(rows)} .wfz files against b2://{args.bucket}/\n", flush=True)

    def check(r: dict) -> dict:
        key = server_to_key(r["wfz"], args.share_root, args.prefix)
        out = {"session": r["session"], "wfz": r["wfz"], "b2_key": key,
               "local_bytes": r["wfz_bytes_now"], "b2_bytes": "", "present": "False",
               "size_matches": "False", "error": ""}
        info, e = b2_json(["file", "info", f"b2://{args.bucket}/{key}"])
        if e:
            out["error"] = e
            return out
        out["present"] = "True"
        out["b2_bytes"] = str(info.get("size", ""))
        out["size_matches"] = str(str(info.get("size")) == str(r["wfz_bytes_now"]))
        return out

    with ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(check, rows))

    fields = list(results[0].keys()) if results else ["session"]
    with Path(args.out).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(results)

    ok = [r for r in results if r["present"] == "True" and r["size_matches"] == "True"]
    bad = [r for r in results if r not in ok]
    print(f"present and right size : {len(ok)}/{len(results)}")
    if bad:
        print(f"NOT CONFIRMED          : {len(bad)}  <- their tars must not be deleted")
        for r in bad[:15]:
            print(f"  {r['session']:<24} {r['error'] or 'size ' + r['b2_bytes']}")
        if len(bad) == len(results):
            print("\nEverything failed the same way - the key or prefix mapping is probably "
                  "wrong rather than the backup being absent. Check --prefix against one "
                  "object name from the B2 console.")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
