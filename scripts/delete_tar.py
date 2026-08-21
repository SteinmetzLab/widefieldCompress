"""Delete one original ``widefield.tar``, only after everything that must be true has been proven.

This is the first tool in the project with a delete path. `wfcompress` itself still has none, by
design, and this script is deliberately separate from it: the compressor can never delete, and the
deleter can never compress.

Three subcommands, in order. Nothing destructive happens without the middle one, and the middle one
refuses unless the first has recently passed on the same bytes.

    python scripts/delete_tar.py check   SESSION --bucket sahalebackup
    python scripts/delete_tar.py delete  SESSION --bucket sahalebackup --confirm SESSION
    python scripts/delete_tar.py offsite SESSION --bucket sahalebackup     # after the 22:00 sync

``check`` is read-only and writes its verdict to ``data/deletion_checks.jsonl``. ``delete`` re-runs
every cheap condition, requires a stored ``check`` pass no older than --max-check-age-h over a tar
of the same size and mtime, and requires --confirm to repeat the session name exactly. ``offsite``
proves the deletion propagated to B2 as a *hide* and that the retained prior version still
reconstructs the original bytes.

The eleven pre-delete conditions, and roughly what each costs. "SMB" reads cross the network from
the share; "B2" downloads from Backblaze. Cheap ones are metadata only.

    C1   run log records this session compressed ok                          cheap
    C2   .wfz exists on the server at the size the log recorded              cheap
    C3   format >= v2 and a source_tar_sha256 exists                         cheap
    C4   the tar is still exactly the size that hash was taken over          cheap
    C5   a receipt exists and claims byte-identity                           cheap
    C6   the local .wfz rebuilds source_tar_sha256 today                     SMB read of the .wfz
    C7   the .wfz is present in B2 at a matching size                        cheap (one API call)
    C8   the tar on disk re-hashes to source_tar_sha256                      SMB read of the tar
    C9   B2's .wfz is byte-identical to the server's                         B2 + SMB read
    C10  B2's .wfz rebuilds source_tar_sha256                                local decode
    C11  B2's widefield.tar hashes to source_tar_sha256                      B2 download of the tar

C11 matters most and is the one the original plan left until after the deletion: that object is
precisely what a restore would pull back, so it is verified *before* anything is removed rather
than afterwards.

C6 and C10 are not redundant. C6 proves the copy on the server works; C10 proves the copy in
Backblaze works. If the server's disk quietly rots, C6 passes and C10 is what saves you.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))

B2BIN = Path(sys.executable).with_name("b2.exe")
CHUNK = 16 << 20  # 16 MB; small reads over SMB are pathologically slow (14 MB/s vs 131 MB/s)
CHECK_LEDGER = HERE / "data" / "deletion_checks.jsonl"
DELETE_LEDGER = HERE / "data" / "deletions.jsonl"


# --------------------------------------------------------------------------- helpers

def sha256_file(path: Path, label: str = "") -> tuple[str, int, float]:
    """Streaming SHA-256. Returns (hexdigest, bytes, seconds)."""
    h, n, t0 = hashlib.sha256(), 0, time.time()
    with open(path, "rb", buffering=0) as fh:
        while True:
            b = fh.read(CHUNK)
            if not b:
                break
            h.update(b)
            n += len(b)
    dt = time.time() - t0
    if label:
        print(f"      read {n/1e9:.2f} GB in {dt:.0f} s ({n/1e6/max(dt,1e-9):.0f} MB/s)  {label}")
    return h.hexdigest(), n, dt


def b2_download(bucket: str, uri: str, dest: Path) -> float:
    dest.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    p = subprocess.run([str(B2BIN), "file", "download", uri, str(dest), "--no-progress"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"b2 download failed for {uri}: {(p.stderr or p.stdout).strip()[-300:]}")
    return time.time() - t0


def b2_stream_sha256(uri: str) -> tuple[str, int, float]:
    """SHA-256 a B2 object without ever writing it to disk.

    Three quarters of the archives have tars over 140 GB, and the full gate would otherwise want
    the tar *and* the .wfz on local disk at once - 622 GB for the largest, against 98 GB free.
    `b2 file cat` streams to stdout, so the hash costs no disk at all and skips a write besides.
    """
    t0 = time.time()
    h, n = hashlib.sha256(), 0
    proc = subprocess.Popen([str(B2BIN), "file", "cat", uri, "--no-progress"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            chunk = proc.stdout.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            n += len(chunk)
    finally:
        proc.stdout.close()
        rc = proc.wait()
        err = proc.stderr.read().decode("utf-8", "replace").strip()
        proc.stderr.close()
    if rc != 0:
        raise RuntimeError(f"b2 file cat failed for {uri}: {err[-300:]}")
    return h.hexdigest(), n, time.time() - t0


def free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(str(path)).free


class Cond:
    """One gate condition and its outcome."""

    def __init__(self, key: str, text: str, cheap: bool):
        self.key, self.text, self.cheap = key, text, cheap
        self.ok: bool | None = None
        self.note = ""
        self.seconds = 0.0
        self.derived = False  # concluded from other conditions rather than measured directly

    def set(self, ok: bool, note: str = "", seconds: float = 0.0, derived: bool = False) -> None:
        self.ok, self.note, self.seconds, self.derived = ok, note, seconds, derived


def jsonl_append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def read_log(log_path: str) -> dict[str, dict]:
    """Every session's most recent successful record, keyed by session."""
    latest: dict[str, dict] = {}
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r.get("ok"):
                latest[r["session"]] = r
    return latest


def server_path(recorded: str, server_root: str) -> Path:
    """Map a path as written in the run log onto this machine's view of the share.

    The log records ``Y:\\Subjects\\...`` because that is how the drive is mapped for the
    compressor. Everything here uses the UNC form, which works whether or not Y: is mounted.
    """
    p = recorded.replace("\\", "/")
    if len(p) > 2 and p[1] == ":":
        return Path(server_root) / p[3:]
    return Path(recorded)


def b2_key(recorded: str, b2_top: str) -> str:
    """The B2 object key for a path recorded in the run log.

    The Cloud Sync task lowercased the top level, so ``Y:\\Subjects\\AL_0033\\...`` becomes
    ``subjects/AL_0033/...``; everything below keeps its case.
    """
    p = recorded.replace("\\", "/")
    i = p.lower().find("/subjects/")
    rel = p[i + len("/subjects/"):] if i >= 0 else p.split("/", 1)[-1]
    return f"{b2_top}/{rel}"


def load_paths(args, rec: dict | None = None) -> dict:
    """Resolve every path for one session.

    **The filenames come from the run log, never from a template.** 463 of 466 archives are
    ``widefield.tar``, but three are named after the experiment number - ``3.tar``, ``1.tar`` -
    and an earlier version of this script hardcoded ``widefield.tar`` and reported those three as
    "the tar is already gone". It failed closed, so nothing was ever deleted wrongly, but it would
    have quietly excluded them from the campaign forever.
    """
    sess = args.session.strip("/").replace("\\", "/")
    if rec is None:
        rec = read_log(args.log).get(sess)
    if rec is None:
        # No log record: fall back to the conventional layout purely so the caller can report C1
        # failing rather than crashing.
        remote = Path(args.server) / "Subjects" / Path(sess)
        tar, wfz = remote / "widefield.tar", remote / "widefield.wfz"
        keys = {"tar": f"{args.b2_top}/{sess}/widefield.tar",
                "wfz": f"{args.b2_top}/{sess}/widefield.wfz"}
    else:
        tar = server_path(rec["tar"], args.server)
        wfz = server_path(rec["wfz"], args.server)
        keys = {"tar": b2_key(rec["tar"], args.b2_top), "wfz": b2_key(rec["wfz"], args.b2_top)}
    return {
        "session": sess,
        "rec": rec,
        "remote": tar.parent,
        "tar": tar,
        "wfz": wfz,
        "receipt": wfz.with_name(wfz.name + ".receipt.json"),
        "b2_tar": keys["tar"],
        "b2_wfz": keys["wfz"],
        "work": Path(args.workdir) / sess.replace("/", "_"),
    }


# --------------------------------------------------------------------------- the gate

def run_gate(args, P: dict, cheap_only: bool = False) -> tuple[list[Cond], dict]:
    from wfcompress import codec

    conds = [
        Cond("C1", "run log records this session compressed ok", True),
        Cond("C2", ".wfz exists on the server at the recorded size", True),
        Cond("C3", "format >= v2 with a source_tar_sha256", True),
        Cond("C4", "the tar is still the size that hash was taken over", True),
        Cond("C5", "a receipt exists and claims byte-identity", True),
        Cond("C6", "the local .wfz rebuilds source_tar_sha256 today", False),
        Cond("C7", "the .wfz is present in B2 at a matching size", True),
        Cond("C8", "the tar on disk re-hashes to source_tar_sha256", False),
        Cond("C9", "B2's .wfz is byte-identical to the server's", False),
        Cond("C10", "B2's .wfz rebuilds source_tar_sha256", False),
        Cond("C11", "B2's widefield.tar hashes to source_tar_sha256", False),
    ]
    C = {c.key: c for c in conds}
    facts: dict = {"session": P["session"]}

    # ---- C1 --------------------------------------------------------------------------
    rec = P.get("rec")
    C["C1"].set(rec is not None, "" if rec else "no ok row in the run log")
    if rec is None:
        return conds, facts
    facts["log_output_bytes"] = rec["output_bytes"]
    facts["log_source_bytes"] = rec["source_bytes"]
    facts["log_tar_sha256"] = rec.get("tar_sha256")

    # ---- C2 --------------------------------------------------------------------------
    wfz_now = P["wfz"].stat().st_size if P["wfz"].exists() else -1
    facts["wfz_bytes_now"] = wfz_now
    C["C2"].set(wfz_now == rec["output_bytes"],
                f"{wfz_now} on disk vs {rec['output_bytes']} recorded"
                if wfz_now != rec["output_bytes"] else "")

    # ---- C3 / C5 ---------------------------------------------------------------------
    receipt = {}
    if P["receipt"].exists():
        receipt = json.loads(P["receipt"].read_text(encoding="utf-8"))
    expect = receipt.get("source_tar_sha256")
    facts["source_tar_sha256"] = expect
    facts["format_version"] = receipt.get("format_version")
    C["C3"].set(bool(expect) and (receipt.get("format_version") or 0) >= 2,
                f"format_version={receipt.get('format_version')} sha={bool(expect)}")
    C["C5"].set(P["receipt"].exists() and receipt.get("byte_identical") is True
                and receipt.get("byte_identical_verified") is True,
                "" if receipt else "no receipt")

    # cross-check: the run log and the receipt must agree about the hash
    if expect and facts.get("log_tar_sha256") and expect != facts["log_tar_sha256"]:
        C["C3"].set(False, "run log and receipt disagree about source_tar_sha256")

    # ---- C4 --------------------------------------------------------------------------
    if P["tar"].exists():
        st = P["tar"].stat()
        facts["tar_bytes_now"] = st.st_size
        facts["tar_mtime"] = int(st.st_mtime)
        C["C4"].set(st.st_size == rec["source_bytes"],
                    f"{st.st_size} on disk vs {rec['source_bytes']} hashed" if
                    st.st_size != rec["source_bytes"] else "")
    else:
        facts["tar_bytes_now"] = -1
        C["C4"].set(False, "the tar is already gone")

    # ---- C7 --------------------------------------------------------------------------
    t0 = time.time()
    b2_wfz_size = None
    try:
        from b2sdk.v2 import B2Api, SqliteAccountInfo
        bucket = B2Api(SqliteAccountInfo()).get_bucket_by_name(args.bucket)
        fi = bucket.get_file_info_by_name(P["b2_wfz"])
        b2_wfz_size = fi.size
        C["C7"].set(b2_wfz_size == rec["output_bytes"],
                    f"B2 has {b2_wfz_size}, server recorded {rec['output_bytes']}"
                    if b2_wfz_size != rec["output_bytes"] else "", time.time() - t0)
    except Exception as e:  # noqa: BLE001
        C["C7"].set(False, f"{type(e).__name__}: {e}"[:140], time.time() - t0)
    facts["b2_wfz_bytes"] = b2_wfz_size

    if cheap_only:
        return conds, facts

    # ---- C6: verify the server's .wfz ------------------------------------------------
    print("\n  [C6] verifying the .wfz on the server (reads it over SMB, writes nothing) ...",
          flush=True)
    t0 = time.time()
    try:
        r = codec.verify(str(P["wfz"]), threads=args.threads, progress=None)
        dt = time.time() - t0
        got = r.get("tar_sha256")
        print(f"      rebuilt {r.get('rebuilt_bytes', 0)/1e9:.2f} GB in {dt:.0f} s")
        C["C6"].set(got == expect and r.get("byte_identical") is True,
                    "" if got == expect else f"got {got}", dt)
    except Exception as e:  # noqa: BLE001
        C["C6"].set(False, f"{type(e).__name__}: {e}"[:140], time.time() - t0)

    # ---- C8: re-hash the tar on disk -------------------------------------------------
    print("\n  [C8] re-hashing the tar on the server ...", flush=True)
    try:
        got, n, dt = sha256_file(P["tar"], "the tar over SMB")
        facts["tar_rehash_sha256"] = got
        C["C8"].set(got == expect, "" if got == expect else f"got {got}", dt)
    except Exception as e:  # noqa: BLE001
        C["C8"].set(False, f"{type(e).__name__}: {e}"[:140])

    # ---- C9 / C10 / C11: pull both objects back out of B2 ----------------------------
    # C9 and C11 stream and never touch disk. C10 must decode, which needs the whole .wfz local
    # (the container's footer offset is at the end, so it is not streamable).
    dl_wfz = P["work"] / "widefield.wfz"
    try:
        print("\n  [C9] streaming the .wfz out of B2 and hashing both copies ...", flush=True)
        b2_sha, b2_n, dt = b2_stream_sha256(f"b2://{args.bucket}/{P['b2_wfz']}")
        print(f"      B2     {b2_sha}  ({b2_n/1e9:.2f} GB in {dt:.0f} s, "
              f"{b2_n/1e6/max(dt,1e-9):.0f} MB/s)")
        srv_sha, srv_n, _ = sha256_file(P["wfz"], "the server copy over SMB")
        print(f"      server {srv_sha}")
        same = b2_sha == srv_sha and b2_n == srv_n
        C["C9"].set(same, "" if same else "the two copies differ", dt)

        need = (b2_n * 11) // 10  # the download plus 10% headroom
        have = free_bytes(P["work"])
        if C["C9"].ok and need > have:
            C["C10"].set(True, f"derived, not measured: identical to the server copy (C9), which "
                               f"rebuilds the hash (C6). Decoding it locally needs "
                               f"{need/1e9:.0f} GB and only {have/1e9:.0f} GB is free.",
                         derived=True)
            print(f"\n  [C10] skipped as a direct measurement: needs {need/1e9:.0f} GB local, "
                  f"{have/1e9:.0f} GB free.")
            print("        Concluded from C9 and C6 instead - the B2 bytes are identical to the")
            print("        server's, and the server's rebuild the recorded hash.")
        else:
            print("\n  [C10] downloading the .wfz to decode it ...", flush=True)
            dt = b2_download(args.bucket, f"b2://{args.bucket}/{P['b2_wfz']}", dl_wfz)
            print(f"      {dl_wfz.stat().st_size/1e9:.2f} GB in {dt:.0f} s")
            t0 = time.time()
            r = codec.verify(str(dl_wfz), threads=args.threads, progress=None)
            dt = time.time() - t0
            got = r.get("tar_sha256")
            print(f"      rebuilt {r.get('rebuilt_bytes', 0)/1e9:.2f} GB in {dt:.0f} s")
            C["C10"].set(got == expect and r.get("byte_identical") is True,
                         "" if got == expect else f"got {got}", dt)
            if not args.keep:
                shutil.rmtree(P["work"], ignore_errors=True)

        print("\n  [C11] streaming the tar out of B2 - the object a restore would pull back ...",
              flush=True)
        got, n, dt = b2_stream_sha256(f"b2://{args.bucket}/{P['b2_tar']}")
        print(f"      {n/1e9:.2f} GB in {dt:.0f} s ({n/1e6/max(dt,1e-9):.0f} MB/s)")
        print(f"      {got}")
        C["C11"].set(got == expect and n == rec["source_bytes"],
                     "" if got == expect else f"got {got}", dt)
    except Exception as e:  # noqa: BLE001
        for k in ("C9", "C10", "C11"):
            if C[k].ok is None:
                C[k].set(False, f"{type(e).__name__}: {e}"[:140])
    finally:
        if not args.keep:
            shutil.rmtree(P["work"], ignore_errors=True)

    return conds, facts


def report(conds: list[Cond]) -> bool:
    print("\n" + "=" * 84)
    total = 0.0
    for c in conds:
        mark = {True: "PASS", False: "FAIL", None: "----"}[c.ok]
        if c.ok is True and c.derived:
            mark = "DERV"
        t = f"{c.seconds:6.0f}s" if c.seconds >= 1 else "      "
        print(f"  {mark}  {c.key:4s} {c.text:56s} {t}"
              + (f"\n           {c.note}" if c.note and (c.ok is not True or c.derived) else ""))
        total += c.seconds
    print("=" * 84)
    every = all(c.ok for c in conds)
    print(f"  {'ALL CONDITIONS PASS' if every else '*** NOT SAFE TO DELETE ***'}"
          f"   ({total:.0f} s of checking)")
    return every


# --------------------------------------------------------------------------- subcommands

def cmd_check(args) -> int:
    cheap = getattr(args, "tier", "full") == "cheap"
    P = load_paths(args)
    print(f"session {P['session']}\ntar     {P['tar']}\n"
          f"b2      b2://{args.bucket}/{P['b2_tar']}\ntier    {'cheap' if cheap else 'full'}\n")
    conds, facts = run_gate(args, P, cheap_only=cheap)
    if cheap:
        conds = [c for c in conds if c.cheap]
    every = report(conds)
    jsonl_append(CHECK_LEDGER, {
        "checked_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session": P["session"], "all_pass": every, "host": platform.node(),
        "tier": "cheap" if cheap else "full",
        "conditions": {c.key: {"ok": c.ok, "note": c.note, "seconds": round(c.seconds, 1),
                               "derived": c.derived} for c in conds},
        **facts,
    })
    print(f"\nverdict appended to {CHECK_LEDGER}")
    if every:
        extra = " --allow-cheap" if cheap else ""
        print("\nNothing has been deleted. To delete, run the same session through:\n"
              f"  python scripts/delete_tar.py --bucket {args.bucket} delete {P['session']} "
              f"--confirm {P['session']}{extra}")
    return 0 if every else 1


def _recent_check(session: str, tar_bytes: int, tar_mtime: int, max_age_h: float,
                  allow_cheap: bool = False) -> dict | None:
    """The most recent passing check over these exact bytes, or None.

    ``allow_cheap`` has to be asked for. The default is that only a full 11-condition check
    authorises a deletion; a cheap-tier check is a screening pass and says nothing about whether
    the .wfz still decodes. Batch deletion deliberately opts in, and the tier that authorised each
    deletion is recorded in the ledger so it can be audited later.
    """
    if not CHECK_LEDGER.exists():
        return None
    best = None
    for line in CHECK_LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("session") != session or not r.get("all_pass"):
            continue
        # records written before tiers existed were all full checks
        if r.get("tier", "full") == "cheap" and not allow_cheap:
            continue
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(r["checked_utc"])).total_seconds() / 3600
        if age > max_age_h:
            continue
        if r.get("tar_bytes_now") != tar_bytes or r.get("tar_mtime") != tar_mtime:
            continue  # not the same bytes the check was made over
        best = r
    return best


def cmd_delete(args) -> int:
    from wfcompress import filelog

    P = load_paths(args)
    if args.confirm != P["session"]:
        print(f"refusing: --confirm must repeat the session name exactly.\n"
              f"  got      {args.confirm!r}\n  expected {P['session']!r}")
        return 2
    if not P["tar"].exists():
        print(f"refusing: no tar at {P['tar']}")
        return 2
    st = P["tar"].stat()

    allow_cheap = getattr(args, "allow_cheap", False)
    prior = _recent_check(P["session"], st.st_size, int(st.st_mtime), args.max_check_age_h,
                          allow_cheap=allow_cheap)
    if prior is None:
        print(f"refusing: no passing {'cheap-or-full' if allow_cheap else 'full'} check for "
              f"{P['session']} within {args.max_check_age_h} h over a tar of this exact size "
              f"and mtime.\n"
              f"Run:  python scripts/delete_tar.py --bucket {args.bucket} check {P['session']}")
        return 2
    tier = prior.get("tier", "full")
    print(f"using the {tier} check from {prior['checked_utc']} "
          f"({'all 11 conditions' if tier == 'full' else 'C1-C5 and C7'} passed)\n")

    print("re-running the cheap conditions immediately before deleting ...")
    conds, facts = run_gate(args, P, cheap_only=True)
    cheap = [c for c in conds if c.cheap]
    if not report(cheap):
        print("\nrefusing: something changed since the check.")
        return 1

    print(f"\nabout to delete {P['tar']}  ({st.st_size/1e9:.2f} GB)")
    print(f"  its .wfz stays at {P['wfz']}")
    print("  B2 keeps a hidden prior version for 30 days (daysFromHidingToDeleting)")
    filelog.record(args.file_log, "delete", P["tar"], st.st_size,
                   note=f"original tar, superseded by widefield.wfz; "
                        f"sha256 {facts.get('source_tar_sha256', '')[:16]}")
    os.remove(P["tar"])
    gone = not P["tar"].exists()
    print(f"\n  {'DELETED' if gone else 'STILL PRESENT - something is wrong'}")

    jsonl_append(DELETE_LEDGER, {
        "deleted_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session": P["session"], "tar": str(P["tar"]), "tar_bytes": st.st_size,
        "source_tar_sha256": facts.get("source_tar_sha256"),
        "wfz": str(P["wfz"]), "wfz_bytes": facts.get("wfz_bytes_now"),
        "check_from": prior["checked_utc"], "check_tier": prior.get("tier", "full"),
        "host": platform.node(), "verified_gone": gone,
    })
    print(f"  recorded in {DELETE_LEDGER}")
    print("\nNext: after the 22:00 Cloud Sync run, prove the offsite copy survived the delete:\n"
          f"  python scripts/delete_tar.py --bucket {args.bucket} offsite {P['session']}")
    return 0 if gone else 1


def cmd_sweep(args) -> int:
    """Run only the cheap conditions across every compressed archive. Deletes nothing.

    This is the tier-1 pass: C1-C5 are local metadata and C7 is one B2 API call, so the whole
    corpus costs minutes rather than the weeks the full gate would. Its job is to surface the
    archives that would be *refused*, before anyone plans a deletion batch around them.
    """
    import csv as _csv
    from concurrent.futures import ThreadPoolExecutor

    latest = read_log(args.log)
    sessions = sorted(latest)
    if args.limit:
        sessions = sessions[:args.limit]
    print(f"{len(sessions)} archives recorded as compressed; running the cheap conditions "
          f"(C1-C5, C7) with {args.workers} workers\n", flush=True)

    def one(sess: str):
        a = argparse.Namespace(**vars(args))
        a.session = sess
        P = load_paths(a, latest[sess])
        try:
            conds, facts = run_gate(a, P, cheap_only=True)
        except Exception as e:  # noqa: BLE001 - one bad archive must not stop the sweep
            return sess, None, None, f"{type(e).__name__}: {e}"[:160]
        return sess, conds, facts, ""

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, out in enumerate(ex.map(one, sessions), 1):
            results.append(out)
            if i % 25 == 0:
                print(f"  ... {i}/{len(sessions)}", flush=True)

    outp = Path(args.out)
    keys = ["C1", "C2", "C3", "C4", "C5", "C7"]
    failed = []
    with outp.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["session", "verdict", *keys, "tar_bytes_now", "wfz_bytes_now",
                    "b2_wfz_bytes", "notes"])
        for sess, conds, facts, err in results:
            if conds is None:
                w.writerow([sess, "ERROR", *[""] * len(keys), "", "", "", err])
                failed.append((sess, f"ERROR {err}"))
                continue
            C = {c.key: c for c in conds}
            bad = [k for k in keys if C[k].ok is not True]
            verdict = "PASS" if not bad else "REFUSE"
            notes = "; ".join(f"{k}: {C[k].note or 'failed'}" for k in bad)
            w.writerow([sess, verdict, *[{True: "pass", False: "fail", None: "n/a"}[C[k].ok]
                                         for k in keys],
                        facts.get("tar_bytes_now", ""), facts.get("wfz_bytes_now", ""),
                        facts.get("b2_wfz_bytes", ""), notes])
            if bad:
                failed.append((sess, notes))

    npass = sum(1 for s, c, f, e in results
                if c is not None and all(x.ok is True for x in c if x.cheap))
    print("\n" + "=" * 84)
    print(f"  PASS   {npass}")
    print(f"  REFUSE {len(failed)}")
    if failed:
        print("\n  every archive that would be refused:")
        for sess, why in failed:
            print(f"    {sess:34s} {why}")
    print("=" * 84)
    print(f"wrote {outp}")
    print("\nNothing was deleted. `sweep` has no delete path.")
    return 0


def cmd_offsite(args) -> int:
    """After the sync: is the B2 object hidden rather than gone, and does it still restore?"""
    from b2sdk.v2 import B2Api, SqliteAccountInfo

    P = load_paths(args)
    key = P["b2_tar"]
    api = B2Api(SqliteAccountInfo())
    bucket = api.get_bucket_by_name(args.bucket)

    versions = [v for v, _ in bucket.ls(key.rsplit("/", 1)[0] + "/", latest_only=False)
                if v.file_name == key]
    print(f"{len(versions)} version(s) of {key}:")
    for v in versions:
        print(f"  {v.action:7s} {v.size:>14} {v.upload_timestamp}  {v.id_}")

    hides = [v for v in versions if v.action == "hide"]
    uploads = [v for v in versions if v.action == "upload"]
    if not hides:
        print("\nNo hide marker yet. Either the sync has not run since the delete, or the task is\n"
              "not in SYNC mode. Re-run after the next 22:00 cycle.")
        return 1
    if not uploads:
        print("\nHidden, but no prior version remains - the retention window has already closed.")
        return 1

    expect = json.loads(P["receipt"].read_text(encoding="utf-8"))["source_tar_sha256"]
    target = max(uploads, key=lambda v: v.upload_timestamp)
    print(f"\nrestoring the retained version {target.id_} ({target.size/1e9:.2f} GB) ...",
          flush=True)
    P["work"].mkdir(parents=True, exist_ok=True)
    dest = P["work"] / "restored_widefield.tar"
    try:
        dt = b2_download(args.bucket, f"b2id://{target.id_}", dest)
        mb = dest.stat().st_size / 1e6
        print(f"  {mb/1000:.2f} GB in {dt:.0f} s ({mb/max(dt,1e-9):.0f} MB/s)")
        got, n, _ = sha256_file(dest, "the restored tar")
        restores = got == expect and n == target.size
        gone = not P["tar"].exists()
        print(f"\n  {got}\n  {'MATCHES' if got == expect else 'DOES NOT MATCH'} "
              f"source_tar_sha256")
        print("\n" + "=" * 84)
        print(f"  {'PASS' if gone else 'FAIL'}  P1  the tar is gone from the server"
              + ("" if gone else "   <-- it is still there; was it restored already?"))
        print("  PASS  P2  B2 shows a hide marker plus a retained prior version")
        print(f"  {'PASS' if restores else 'FAIL'}  P3  the retained version restores to "
              f"source_tar_sha256")
        print("=" * 84)
        every = gone and restores
        if every:
            print("  The undo path is demonstrated, not assumed.")
        return 0 if every else 1
    finally:
        if not args.keep:
            shutil.rmtree(P["work"], ignore_errors=True)


# --------------------------------------------------------------------------- cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=r"\\sahale.biostr.washington.edu\data")
    ap.add_argument("--b2-top", default="subjects")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--log", default=str(HERE / "data" / "bulk.jsonl"))
    ap.add_argument("--file-log", default=str(HERE / "data" / "fileEditLog.csv"))
    ap.add_argument("--workdir", default=r"D:\temp\tar_deletion")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--keep", action="store_true", help="retain downloads for inspection")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="run the pre-delete conditions; read-only")
    c.add_argument("session")
    c.add_argument("--tier", choices=("full", "cheap"), default="full",
                   help="full runs all 11 conditions; cheap runs only C1-C5 and C7")
    c.set_defaults(func=cmd_check)

    d = sub.add_parser("delete", help="delete the tar, if a recent check passed")
    d.add_argument("session")
    d.add_argument("--confirm", required=True, help="repeat the session name exactly")
    d.add_argument("--max-check-age-h", type=float, default=24.0)
    d.add_argument("--allow-cheap", action="store_true",
                   help="accept a cheap-tier check as authorisation (default: full checks only)")
    d.set_defaults(func=cmd_delete)

    o = sub.add_parser("offsite", help="after the sync: prove B2 hid it and can restore it")
    o.add_argument("session")
    o.set_defaults(func=cmd_offsite)

    s = sub.add_parser("sweep", help="cheap conditions only, across every archive; deletes nothing")
    s.add_argument("--out", default=str(HERE / "data" / "cheap_sweep.csv"))
    s.add_argument("--workers", type=int, default=12)
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_sweep)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
