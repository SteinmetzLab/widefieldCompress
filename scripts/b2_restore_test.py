"""Step 0: prove the Backblaze copy is a real, restorable archive - without deleting anything.

`docs/B2_RESTORE_TEST.md` proposes deleting a tar and restoring it from B2's prior-version
history. That tests two different things at once, and only one of them needs a deletion:

  * **Is the offsite copy intact and correct?**  Answered by downloading it and hashing it.
    No deletion required. This script does that.
  * **Does B2's 60-day version window return a file after it is deleted?**  That one genuinely
    needs a delete, a sync cycle, and a restore. This script does *not* do it.

Doing the second before the first would be backwards: it would delete a copy on the strength of
an offsite backup nobody had ever read back.

Three independent checks, on one session:

  A. the `widefield.tar` in B2 hashes to the receipt's ``source_tar_sha256``
     -> the offsite original is intact and restorable today.
  B. the `widefield.wfz` in B2 is byte-identical to the one on the server
     -> the sync transferred the replacement correctly, not merely a file of the right size.
  C. the *downloaded* `.wfz` reconstructs ``source_tar_sha256``
     -> the offsite replacement is a working archive on its own, independent of the server.

C is the one that matters for deletion. A and B are what make C interpretable: without B, a pass
on C would only prove the local file is fine.

Read-only against both the server and B2 - the B2 key has no delete capability. The only writes
are downloads into --workdir.

    python scripts/b2_restore_test.py --session AL_0033/2025-03-17/1 --bucket sahalebackup
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))

B2 = Path(sys.executable).with_name("b2.exe")
CHUNK = 16 << 20  # 16 MB, matching codec.WRITE_BUFFER; small reads over SMB are pathologically slow


def sha256_file(path: Path) -> tuple[str, int]:
    """Streaming SHA-256. Returns (hexdigest, bytes read)."""
    h, n = hashlib.sha256(), 0
    with open(path, "rb", buffering=0) as fh:
        while True:
            b = fh.read(CHUNK)
            if not b:
                break
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def b2_download(bucket: str, key: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(B2), "file", "download", f"b2://{bucket}/{key}", str(dest), "--no-progress"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"b2 download failed for {key}: "
                           f"{(p.stderr or p.stdout).strip()[-400:]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", required=True, help="e.g. AL_0033/2025-03-17/1")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--server", default=r"\\sahale.biostr.washington.edu\data")
    ap.add_argument("--b2-top", default="subjects",
                    help="the Cloud Sync task lowercased the top level; see check_b2_presence.py")
    ap.add_argument("--workdir", default=r"D:\temp\b2_restore_test")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--keep", action="store_true", help="do not delete the downloads at the end")
    args = ap.parse_args()

    from wfcompress import codec

    sess = args.session.strip("/").replace("\\", "/")
    remote = Path(args.server) / "Subjects" / Path(sess)
    work = Path(args.workdir) / sess.replace("/", "_")
    work.mkdir(parents=True, exist_ok=True)

    print(f"session   {sess}")
    print(f"server    {remote}")
    print(f"bucket    b2://{args.bucket}/{args.b2_top}/{sess}/")
    print(f"workdir   {work}\n")

    receipt_path = remote / "widefield.wfz.receipt.json"
    if not receipt_path.exists():
        print(f"FAIL: no receipt at {receipt_path}")
        return 2
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    expect_tar = receipt.get("source_tar_sha256")
    if not expect_tar:
        print("FAIL: receipt has no source_tar_sha256 (format < v2); pick another session")
        return 2
    print(f"receipt says the original tar is {receipt['source_bytes']/1e9:.2f} GB, "
          f"sha256 {expect_tar[:16]}...")
    print(f"and the .wfz is {receipt['output_bytes']/1e9:.2f} GB\n")

    results: list[tuple[str, bool, str]] = []

    # ---- download both objects from B2 -------------------------------------------------
    dl_tar = work / "widefield.tar"
    dl_wfz = work / "widefield.wfz"
    for key_name, dest in (("widefield.tar", dl_tar), ("widefield.wfz", dl_wfz)):
        key = f"{args.b2_top}/{sess}/{key_name}"
        t0 = time.time()
        print(f"downloading b2://{args.bucket}/{key} ...", flush=True)
        try:
            b2_download(args.bucket, key, dest)
        except RuntimeError as e:
            print(f"  FAILED: {e}")
            results.append((f"download {key_name}", False, str(e)[:120]))
            continue
        dt = time.time() - t0
        mb = dest.stat().st_size / 1e6
        print(f"  {mb/1000:.2f} GB in {dt:.0f} s ({mb/max(dt,1e-9):.0f} MB/s)")

    # ---- check A: does the B2 tar hash to what the receipt recorded? -------------------
    print("\n[A] hashing the tar downloaded from B2 ...", flush=True)
    if dl_tar.exists():
        got, n = sha256_file(dl_tar)
        ok = (got == expect_tar) and (n == receipt["source_bytes"])
        print(f"    {got}")
        print(f"    {'MATCHES' if got == expect_tar else 'DOES NOT MATCH'} source_tar_sha256; "
              f"{n} bytes vs {receipt['source_bytes']} recorded")
        results.append(("A  B2's widefield.tar hashes to source_tar_sha256", ok,
                        got if not ok else ""))
    else:
        results.append(("A  B2's widefield.tar hashes to source_tar_sha256", False,
                        "download missing"))

    # ---- check B: is the B2 .wfz byte-identical to the server's? ----------------------
    print("\n[B] hashing both copies of the .wfz ...", flush=True)
    if dl_wfz.exists():
        b2_sha, b2_n = sha256_file(dl_wfz)
        print(f"    B2     {b2_sha}  ({b2_n} bytes)", flush=True)
        srv = remote / "widefield.wfz"
        srv_sha, srv_n = sha256_file(srv)
        print(f"    server {srv_sha}  ({srv_n} bytes)")
        ok = b2_sha == srv_sha and b2_n == srv_n
        print(f"    {'IDENTICAL' if ok else '*** DIFFERENT ***'}")
        results.append(("B  B2's .wfz is byte-identical to the server's", ok, ""))
    else:
        results.append(("B  B2's .wfz is byte-identical to the server's", False,
                        "download missing"))

    # ---- check C: does the DOWNLOADED .wfz rebuild the original tar? -------------------
    print("\n[C] rebuilding the tar from the .wfz downloaded from B2 (writes nothing) ...",
          flush=True)
    if dl_wfz.exists():
        t0 = time.time()
        r = codec.verify(str(dl_wfz), threads=args.threads, progress=None)
        dt = time.time() - t0
        got = r.get("tar_sha256")
        ok = got == expect_tar and r.get("byte_identical") is True
        print(f"    rebuilt {r.get('rebuilt_bytes', 0)/1e9:.2f} GB in {dt:.0f} s")
        print(f"    {got}")
        print(f"    {'MATCHES' if got == expect_tar else 'DOES NOT MATCH'} source_tar_sha256")
        results.append(("C  B2's .wfz rebuilds the original tar byte-for-byte", ok,
                        got if not ok else ""))
    else:
        results.append(("C  B2's .wfz rebuilds the original tar byte-for-byte", False,
                        "download missing"))

    # ---- verdict ----------------------------------------------------------------------
    print("\n" + "=" * 78)
    for name, ok, note in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({note})" if note else ""))
    print("=" * 78)
    every = all(ok for _, ok, _ in results)
    if every:
        print("All checks passed. The offsite copy of this session is intact, complete, and\n"
              "reconstructs the original archive without reference to the server.")
        print("\nStill untested, and it needs a deletion: whether B2's 60-day prior-version\n"
              "window returns a file after the sync propagates a delete. See\n"
              "docs/B2_RESTORE_TEST.md.")
    else:
        print("SOMETHING FAILED. Do not delete anything. Read the lines above.")

    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
        print(f"\nremoved {work}  (--keep to retain)")
    return 0 if every else 1


if __name__ == "__main__":
    raise SystemExit(main())
