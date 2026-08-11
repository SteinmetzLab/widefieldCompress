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


def sdk_bucket(bucket: str):
    """An authorized bucket handle via b2sdk, reusing whatever `b2 account authorize` cached.

    The CLI works, but every lookup is a fresh process paying a Python interpreter start plus a
    b2sdk import -- several seconds each, and 212 of them. The SDK does the same job in one
    process against one session. It reads the cached credentials itself; nothing here ever sees
    the key.
    """
    try:
        from b2sdk.v2 import B2Api, SqliteAccountInfo
    except ImportError:
        return None, "b2sdk not importable"
    try:
        api = B2Api(SqliteAccountInfo())
        return api.get_bucket_by_name(bucket), ""
    except Exception as e:  # noqa: BLE001 - any failure here means fall back to the CLI
        return None, f"{type(e).__name__}: {e}"


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


def top_level_map(bucket: str) -> dict[str, str]:
    """Case-insensitive map of the bucket's top-level names to their real spelling.

    The Cloud Sync Task lowercased the top level: the share's ``Subjects`` is ``subjects`` in B2,
    alongside ``code`` and ``alyx-backup``. Everything below keeps its case. Object names are
    case-sensitive, so looking up ``Subjects/...`` silently finds nothing - which would read as
    "the backup is missing" rather than "the name is wrong". Discovering the real spelling removes
    that whole class of false alarm.
    """
    p = subprocess.run([str(B2), "ls", f"b2://{bucket}/"],
                       capture_output=True, text=True, check=False)
    out: dict[str, str] = {}
    if p.returncode == 0:
        for line in p.stdout.splitlines():
            name = line.strip().rstrip("/")
            if name and "/" not in name:
                out[name.lower()] = name
    return out


def server_to_key(path: str, share_root: str, prefix: str,
                  tops: dict[str, str] | None = None) -> str:
    """Map a share path to the object name the sync task would have given it.

    Anchoring on a known top-level name rather than on the share root, because the same file is
    reachable as ``Y:\\Subjects\\...`` or ``\\\\sahale...\\data\\Subjects\\...`` and the batch log
    contains both. Stripping a fixed prefix silently left ``Y:/Subjects/...`` as the object name
    for half the corpus, which then read as "missing from the backup" - a false alarm that looks
    exactly like the real thing this check is for.
    """
    parts = [seg for seg in path.replace("\\", "/").split("/") if seg]
    if tops:
        for i, seg in enumerate(parts):
            if seg.lower() in tops:
                rel = "/".join([tops[seg.lower()], *parts[i + 1:]])
                break
        else:
            rel = "/".join(parts)
    else:
        p = "/".join(parts)
        root = share_root.lower().replace("\\", "/").strip("/")
        rel = p[len(root):].lstrip("/") if p.lower().startswith(root) else p
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

    tops = top_level_map(args.bucket)
    if tops:
        print(f"top-level names in the bucket: {', '.join(sorted(tops.values()))}")

    rows = [r for r in csv.DictReader(Path(args.audit).open(encoding="utf-8"))
            if r["verdict"] == "SAFE"]
    if args.limit:
        rows = rows[: args.limit]
    print(f"checking {len(rows)} .wfz files against b2://{args.bucket}/\n", flush=True)

    bucket, bucket_err = sdk_bucket(args.bucket)
    print("lookups via b2sdk" if bucket else f"lookups via the CLI ({bucket_err})")

    def size_of(key: str) -> tuple[int | None, str]:
        if bucket is not None:
            try:
                return bucket.get_file_info_by_name(key).size, ""
            except Exception as e:  # noqa: BLE001 - "not found" arrives as an exception too
                return None, f"{type(e).__name__}: {str(e)[:120]}"
        info, e = b2_json(["file", "info", f"b2://{args.bucket}/{key}"])
        return (None, e) if e else (info.get("size"), "")

    def check(r: dict) -> dict:
        key = server_to_key(r["wfz"], args.share_root, args.prefix, tops)
        out = {"session": r["session"], "wfz": r["wfz"], "b2_key": key,
               "local_bytes": r["wfz_bytes_now"], "b2_bytes": "", "present": "False",
               "size_matches": "False", "error": ""}
        size, e = size_of(key)
        if e:
            out["error"] = e
            return out
        out["present"] = "True"
        out["b2_bytes"] = str(size)
        out["size_matches"] = str(str(size) == str(r["wfz_bytes_now"]))
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
