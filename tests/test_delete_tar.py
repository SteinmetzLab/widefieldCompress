"""The gate in front of the only delete path in this project.

`scripts/delete_tar.py` is the one tool here that can destroy data. These tests exist to prove it
refuses in every circumstance where it should, which matters more than proving it succeeds: a
false refusal costs a rerun, a false deletion costs an irreplaceable recording.

Everything here runs against a fake session tree in ``tmp_path``. Nothing touches the share, B2, or
the real ledgers. The expensive conditions (C6, C8-C11) read whole files and download from
Backblaze, so they are not exercised here; what is tested is the cheap gate, the freshness rules
that decide whether an expensive check may be reused, and the confirmation guard.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location("delete_tar", ROOT / "scripts" / "delete_tar.py")
dt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dt)


TAR_BODY = b"not really a tar, but it hashes like one" * 512
TAR_SHA = hashlib.sha256(TAR_BODY).hexdigest()
WFZ_BODY = b"pretend wfz payload" * 97


class Args:
    """Stands in for the argparse namespace; only the fields the gate reads."""

    def __init__(self, root: Path, session: str, bucket: str = "testbucket"):
        self.server = str(root)
        self.b2_top = "subjects"
        self.bucket = bucket
        self.session = session
        self.log = str(root / "bulk.jsonl")
        self.file_log = str(root / "fileEditLog.csv")
        self.workdir = str(root / "work")
        self.threads = 1
        self.keep = False


def _write_session(tmp_path, session, stem, extra_rows=()):
    """Build a fake share holding one consistent session whose files are named `stem`.tar/.wfz.

    `stem` is parameterised because 3 of the 466 real archives are named after the experiment
    number (`3.tar`) rather than `widefield.tar`, and the tool must read the name from the run log
    rather than assume one.
    """
    d = tmp_path / "Subjects" / Path(session)
    d.mkdir(parents=True)
    (d / f"{stem}.tar").write_bytes(TAR_BODY)
    (d / f"{stem}.wfz").write_bytes(WFZ_BODY)
    (d / f"{stem}.wfz.receipt.json").write_text(json.dumps({
        "format_version": 2, "byte_identical": True, "byte_identical_verified": True,
        "source_tar_sha256": TAR_SHA, "source_bytes": len(TAR_BODY),
        "output_bytes": len(WFZ_BODY),
    }), encoding="utf-8")
    rows = [{
        "session": session, "ok": True, "source_bytes": len(TAR_BODY),
        "output_bytes": len(WFZ_BODY), "tar_sha256": TAR_SHA,
        # the real log records Y:-relative Windows paths; server_path() maps them onto --server
        "tar": rf"Y:\Subjects\{session.replace('/', chr(92))}\{stem}.tar",
        "wfz": rf"Y:\Subjects\{session.replace('/', chr(92))}\{stem}.wfz",
    }, *extra_rows]
    (tmp_path / "bulk.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return tmp_path, session, d


@pytest.fixture()
def tree(tmp_path):
    """A fake share holding one fully consistent session, conventionally named."""
    return _write_session(tmp_path, "AL_9999/2026-01-02/1", "widefield")


@pytest.fixture()
def oddly_named_tree(tmp_path):
    """The same, but named `3.tar` / `3.wfz` like AL_0033/2025-02-24/3 really is."""
    return _write_session(tmp_path, "AL_9998/2026-01-02/3", "3")


def cheap(tree, mutate=None):
    """Run the cheap half of the gate, optionally mutating the tree first."""
    root, session, d = tree
    if mutate:
        mutate(d, root)
    args = Args(root, session)
    conds, facts = dt.run_gate(args, dt.load_paths(args), cheap_only=True)
    return {c.key: c for c in conds}, facts


# --------------------------------------------------------------------------- the cheap gate

def test_consistent_session_passes_every_cheap_condition(tree):
    C, facts = cheap(tree)
    for key in ("C1", "C2", "C3", "C4", "C5"):
        assert C[key].ok is True, f"{key} failed: {C[key].note}"
    assert facts["source_tar_sha256"] == TAR_SHA
    # C7 talks to B2 and is expected to fail with no credentials; it must fail closed, not crash.
    assert C["C7"].ok is False


def test_an_oddly_named_archive_passes_the_cheap_gate(oddly_named_tree):
    """Regression: an earlier version hardcoded `widefield.tar` and reported the three real
    archives named `3.tar` / `1.tar` as "the tar is already gone" - a false refusal that would
    have silently excluded them from the campaign. Paths must come from the run log."""
    C, facts = cheap(oddly_named_tree)
    for key in ("C1", "C2", "C3", "C4", "C5"):
        assert C[key].ok is True, f"{key} failed: {C[key].note}"


def test_paths_and_b2_keys_come_from_the_log_not_a_template(oddly_named_tree):
    root, session, d = oddly_named_tree
    args = Args(root, session)
    P = dt.load_paths(args)
    assert P["tar"].name == "3.tar"
    assert P["wfz"].name == "3.wfz"
    assert P["receipt"].name == "3.wfz.receipt.json"
    assert P["b2_tar"] == f"subjects/{session}/3.tar"
    assert P["b2_wfz"] == f"subjects/{session}/3.wfz"


def test_b2_key_lowercases_only_the_top_level():
    assert dt.b2_key(r"Y:\Subjects\AL_0033\2025-02-24\3\3.wfz", "subjects") == \
        "subjects/AL_0033/2025-02-24/3/3.wfz"
    # a session that sits directly under Subjects with no experiment subdirectory
    assert dt.b2_key(r"Y:\Subjects\test\2025-11-04\1.tar", "subjects") == \
        "subjects/test/2025-11-04/1.tar"


def test_server_path_maps_the_drive_letter_onto_the_share():
    got = dt.server_path(r"Y:\Subjects\AL_0033\2025-02-24\3\3.tar", r"\\host\data")
    assert got == Path(r"\\host\data") / "Subjects/AL_0033/2025-02-24/3/3.tar"


def test_missing_run_log_row_stops_everything(tree):
    root, session, d = tree
    (root / "bulk.jsonl").write_text("", encoding="utf-8")
    args = Args(root, session)
    conds, _ = dt.run_gate(args, dt.load_paths(args), cheap_only=True)
    C = {c.key: c for c in conds}
    assert C["C1"].ok is False
    # and it must not go on to claim anything else passed
    assert all(c.ok is None for c in conds if c.key != "C1")


def test_a_tar_that_changed_size_fails_c4(tree):
    C, _ = cheap(tree, lambda d, r: (d / "widefield.tar").write_bytes(TAR_BODY + b"extra"))
    assert C["C4"].ok is False
    assert "on disk" in C["C4"].note


def test_an_already_deleted_tar_fails_c4(tree):
    C, _ = cheap(tree, lambda d, r: (d / "widefield.tar").unlink())
    assert C["C4"].ok is False
    assert "already gone" in C["C4"].note


def test_a_wfz_of_the_wrong_size_fails_c2(tree):
    C, _ = cheap(tree, lambda d, r: (d / "widefield.wfz").write_bytes(WFZ_BODY + b"!"))
    assert C["C2"].ok is False


def test_a_missing_wfz_fails_c2(tree):
    C, _ = cheap(tree, lambda d, r: (d / "widefield.wfz").unlink())
    assert C["C2"].ok is False


def test_format_v1_fails_c3(tree):
    def mutate(d, r):
        p = d / "widefield.wfz.receipt.json"
        j = json.loads(p.read_text())
        j["format_version"] = 1
        p.write_text(json.dumps(j))
    C, _ = cheap(tree, mutate)
    assert C["C3"].ok is False


def test_a_receipt_with_no_hash_fails_c3(tree):
    def mutate(d, r):
        p = d / "widefield.wfz.receipt.json"
        j = json.loads(p.read_text())
        del j["source_tar_sha256"]
        p.write_text(json.dumps(j))
    C, _ = cheap(tree, mutate)
    assert C["C3"].ok is False


def test_receipt_and_run_log_disagreeing_about_the_hash_fails_c3(tree):
    """The two records of the same hash must agree. If they do not, something is confused about
    which archive this is, and that is exactly when not to delete."""
    def mutate(d, r):
        p = d / "widefield.wfz.receipt.json"
        j = json.loads(p.read_text())
        j["source_tar_sha256"] = "0" * 64
        p.write_text(json.dumps(j))
    C, _ = cheap(tree, mutate)
    assert C["C3"].ok is False
    assert "disagree" in C["C3"].note


def test_a_receipt_not_claiming_byte_identity_fails_c5(tree):
    def mutate(d, r):
        p = d / "widefield.wfz.receipt.json"
        j = json.loads(p.read_text())
        j["byte_identical_verified"] = False
        p.write_text(json.dumps(j))
    C, _ = cheap(tree, mutate)
    assert C["C5"].ok is False


def test_a_missing_receipt_fails_c3_and_c5(tree):
    C, _ = cheap(tree, lambda d, r: (d / "widefield.wfz.receipt.json").unlink())
    assert C["C3"].ok is False
    assert C["C5"].ok is False


# --------------------------------------------------------------------------- reuse freshness

def _ledger(tmp_path, monkeypatch, rows):
    p = tmp_path / "deletion_checks.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr(dt, "CHECK_LEDGER", p)
    return p


def _row(session="S/1/1", *, ok=True, age_h=1.0, size=100, mtime=12345):
    when = datetime.now(timezone.utc) - timedelta(hours=age_h)
    return {"checked_utc": when.isoformat(timespec="seconds"), "session": session,
            "all_pass": ok, "tar_bytes_now": size, "tar_mtime": mtime}


def test_a_fresh_passing_check_is_reusable(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_row()])
    assert dt._recent_check("S/1/1", 100, 12345, 24.0) is not None


def test_a_stale_check_is_not_reusable(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_row(age_h=48)])
    assert dt._recent_check("S/1/1", 100, 12345, 24.0) is None


def test_a_failing_check_is_never_reusable(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_row(ok=False)])
    assert dt._recent_check("S/1/1", 100, 12345, 24.0) is None


def test_a_check_over_a_different_size_is_not_reusable(tmp_path, monkeypatch):
    """The whole point of the size and mtime match: the evidence must be about these bytes."""
    _ledger(tmp_path, monkeypatch, [_row(size=999)])
    assert dt._recent_check("S/1/1", 100, 12345, 24.0) is None


def test_a_check_over_a_different_mtime_is_not_reusable(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_row(mtime=99999)])
    assert dt._recent_check("S/1/1", 100, 12345, 24.0) is None


def test_another_sessions_check_is_not_reusable(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_row(session="OTHER/1/1")])
    assert dt._recent_check("S/1/1", 100, 12345, 24.0) is None


def test_no_ledger_at_all_is_not_reusable(tmp_path, monkeypatch):
    monkeypatch.setattr(dt, "CHECK_LEDGER", tmp_path / "does_not_exist.jsonl")
    assert dt._recent_check("S/1/1", 100, 12345, 24.0) is None


# --------------------------------------------------------------------------- the delete guard

def test_delete_refuses_when_confirm_does_not_match(tree, monkeypatch, capsys):
    root, session, d = tree
    monkeypatch.setattr(dt, "CHECK_LEDGER", root / "checks.jsonl")
    rc = dt.main(["--bucket", "testbucket", "--server", str(root),
                  "delete", session, "--confirm", "AL_9999/2026-01-02/2"])
    assert rc == 2
    assert "refusing" in capsys.readouterr().out
    assert (d / "widefield.tar").exists(), "the tar must still be there"


def test_delete_refuses_with_no_prior_check(tree, monkeypatch, capsys):
    root, session, d = tree
    monkeypatch.setattr(dt, "CHECK_LEDGER", root / "checks.jsonl")
    rc = dt.main(["--bucket", "testbucket", "--server", str(root),
                  "delete", session, "--confirm", session])
    assert rc == 2
    assert "no passing `check`" in capsys.readouterr().out
    assert (d / "widefield.tar").exists()


def test_delete_refuses_when_the_tar_changed_since_the_check(tree, monkeypatch, capsys):
    """A passing check plus a tar that is no longer those bytes must not delete."""
    root, session, d = tree
    st = (d / "widefield.tar").stat()
    _ledger(root, monkeypatch, [{
        "checked_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session": session, "all_pass": True,
        "tar_bytes_now": st.st_size + 1, "tar_mtime": int(st.st_mtime)}])
    rc = dt.main(["--bucket", "testbucket", "--server", str(root),
                  "delete", session, "--confirm", session])
    assert rc == 2
    assert (d / "widefield.tar").exists()


def test_delete_refuses_when_a_cheap_condition_regressed(tree, monkeypatch, capsys):
    """Even with a valid stored check, the cheap conditions are re-run at the last moment. Here
    C2 has regressed - the .wfz was truncated after the check - so nothing may be removed."""
    root, session, d = tree
    st = (d / "widefield.tar").stat()
    _ledger(root, monkeypatch, [{
        "checked_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session": session, "all_pass": True,
        "tar_bytes_now": st.st_size, "tar_mtime": int(st.st_mtime)}])
    (d / "widefield.wfz").write_bytes(b"truncated")
    rc = dt.main(["--bucket", "testbucket", "--server", str(root),
                  "delete", session, "--confirm", session])
    assert rc == 1
    assert (d / "widefield.tar").exists(), "a regressed .wfz must stop the deletion"


def test_the_only_removal_in_the_module_is_the_tar_itself():
    """A blunt guard against scope creep. os.remove/unlink/rmtree must never be pointed at
    anything but the tar path and the temporary download directory."""
    src = (ROOT / "scripts" / "delete_tar.py").read_text(encoding="utf-8")
    assert src.count("os.remove(") == 1
    assert 'os.remove(P["tar"])' in src
    assert ".unlink(" not in src
    # rmtree only ever clears the download workdir
    for line in src.splitlines():
        if "rmtree" in line:
            assert 'P["work"]' in line, line


# --------------------------------------------------------------------------- streaming from B2

class _FakeProc:
    """Stands in for `b2 file cat`, so the chunked reader can be tested without a network."""

    def __init__(self, body: bytes, rc: int = 0, err: bytes = b""):
        import io
        self.stdout = io.BytesIO(body)
        self.stderr = io.BytesIO(err)
        self._rc = rc

    def wait(self):
        return self._rc


def test_b2_stream_sha256_hashes_the_whole_stream(monkeypatch):
    body = bytes(range(256)) * 9000  # larger than one CHUNK would be if CHUNK were small
    monkeypatch.setattr(dt.subprocess, "Popen", lambda *a, **k: _FakeProc(body))
    got, n, _ = dt.b2_stream_sha256("b2://bucket/key")
    assert got == hashlib.sha256(body).hexdigest()
    assert n == len(body)


def test_b2_stream_sha256_reassembles_across_chunk_boundaries(monkeypatch):
    """The reader loops on a fixed chunk size; a body that is not a multiple of it must still
    hash correctly, and nothing may be dropped or duplicated at the seams."""
    monkeypatch.setattr(dt, "CHUNK", 1000)
    body = b"x" * 2500 + b"y" * 7  # deliberately not a multiple of CHUNK
    monkeypatch.setattr(dt.subprocess, "Popen", lambda *a, **k: _FakeProc(body))
    got, n, _ = dt.b2_stream_sha256("b2://bucket/key")
    assert got == hashlib.sha256(body).hexdigest()
    assert n == 2507


def test_b2_stream_sha256_raises_on_nonzero_exit(monkeypatch):
    """A failed download must raise, not silently return the hash of a truncated stream - that
    would be a hash mismatch reported as a corrupt archive, sending someone down the wrong path."""
    monkeypatch.setattr(dt.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(b"partial", rc=1, err=b"boom: no such file"))
    with pytest.raises(RuntimeError, match="b2 file cat failed"):
        dt.b2_stream_sha256("b2://bucket/missing")


# --------------------------------------------------------------------------- derived conditions

def test_a_derived_condition_still_counts_as_passing_and_is_labelled(capsys):
    """C10 is concluded from C9 and C6 when the .wfz is too big to decode locally. It must count
    as a pass for the gate, but must not be presented as a direct measurement."""
    c = dt.Cond("C10", "B2's .wfz rebuilds the hash", False)
    c.set(True, "derived, not measured: identical to the server copy", derived=True)
    assert dt.report([c]) is True
    out = capsys.readouterr().out
    assert "DERV" in out
    assert "PASS  C10" not in out
    assert "derived, not measured" in out


def test_a_plain_pass_is_not_labelled_derived(capsys):
    c = dt.Cond("C6", "the local .wfz rebuilds the hash", False)
    c.set(True)
    assert dt.report([c]) is True
    out = capsys.readouterr().out
    assert "PASS" in out and "DERV" not in out


# --------------------------------------------------------------------------- the cheap sweep

def test_sweep_writes_a_csv_and_never_deletes(tree, tmp_path, capsys):
    root, session, d = tree
    out = tmp_path / "sweep.csv"
    rc = dt.main(["--bucket", "testbucket", "--server", str(root),
                  "--log", str(root / "bulk.jsonl"),
                  "sweep", "--out", str(out), "--workers", "2"])
    assert rc == 0
    assert (d / "widefield.tar").exists(), "sweep must never remove anything"
    text = out.read_text(encoding="utf-8")
    assert session in text
    # C7 cannot reach B2 in the test environment, so the row must be a REFUSE, not a PASS
    assert "REFUSE" in text
    assert "Nothing was deleted" in capsys.readouterr().out


def test_sweep_flags_an_archive_whose_tar_changed_size(tree, tmp_path, capsys):
    root, session, d = tree
    (d / "widefield.tar").write_bytes(TAR_BODY + b"grew")
    out = tmp_path / "sweep.csv"
    dt.main(["--bucket", "testbucket", "--server", str(root),
             "--log", str(root / "bulk.jsonl"),
             "sweep", "--out", str(out), "--workers", "1"])
    rows = out.read_text(encoding="utf-8").splitlines()
    body = [r for r in rows[1:] if r.strip()]
    assert len(body) == 1
    assert "REFUSE" in body[0]
    assert "C4" in body[0]


def test_sha256_file_matches_hashlib(tmp_path):
    p = tmp_path / "blob"
    body = bytes(range(256)) * 5000
    p.write_bytes(body)
    got, n, _ = dt.sha256_file(p)
    assert got == hashlib.sha256(body).hexdigest()
    assert n == len(body)
