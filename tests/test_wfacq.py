"""wfAcq session folders (wf.bin plus its small files) to .wfz and back. Synthetic data only.

The fixture is built from FORMAT.md in the wfAcq repository, not from wfacq's own writer, so these
tests also check that our reading of the format matches the document. One test cross-checks against
``wfacq.load_session`` when wfacq is importable.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

import numpy as np
import pytest

from wfcompress import cli, codec, container, extract, read_meta, verify
from wfcompress import wfacq as wa
from wfcompress.reader import WfzReader

# FORMAT.md, "Frames table": packed, little-endian, 30 bytes. Declared here rather than imported,
# so a change on either side shows up as a test failure instead of silently agreeing with itself.
RECORD = np.dtype(
    [
        ("frame_counter", "<u8"),
        ("block_id", "<u8"),
        ("camera_timestamp", "<u8"),
        ("channel", "i1"),
        ("flags", "u1"),
        ("roi_mean", "<f4"),
    ]
)
assert RECORD.itemsize == 30

INCOMPLETE, DARK = 5, 9  # frame indices with special content in the fixture


def sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_stack(n=13, h=40, w=48, seed=0):
    """12-bit frames as the camera delivers them, unshifted, plus two awkward ones."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w]
    base = (900 + 600 * np.sin(x / 7) * np.cos(y / 5)).astype(np.int32)
    stack = np.stack(
        [np.clip(base + 3 * i + rng.integers(0, 40, (h, w)), 0, 4095) for i in range(n)]
    ).astype("<u2")
    # An incomplete frame has undefined content (FORMAT.md, "wf.bin"); garbage must round-trip.
    stack[INCOMPLETE] = 0xFFFF
    stack[INCOMPLETE, ::3] = rng.integers(0, 65536, (len(range(0, h, 3)), w))
    stack[DARK] = 0  # lens cap or LEDs off
    return stack


def make_session(root, n=13, h=40, w=48, age_s=7200.0, meta=None, stack=None, pfs=True):
    """A cleanly closed wfAcq session in ``root``, every file aged ``age_s`` seconds."""
    root.mkdir(parents=True, exist_ok=True)
    stack = make_stack(n, h, w) if stack is None else stack
    n = len(stack)
    stack.astype("<u2").tofile(root / "wf.bin")

    table = np.zeros(n, dtype=RECORD)
    table["frame_counter"] = np.arange(n) + 1
    table["block_id"] = np.arange(n) + 1
    table["camera_timestamp"] = (np.arange(n) * 14_285_714).astype(np.uint64)
    table["channel"] = np.arange(n) % 2
    table["roi_mean"] = stack.reshape(n, -1).mean(axis=1) if n else []
    if n > INCOMPLETE:
        table["flags"][INCOMPLETE] = 7
        table["frame_counter"][INCOMPLETE] = 2**64 - 1
        table["camera_timestamp"][INCOMPLETE] = 2**64 - 1
        table["channel"][INCOMPLETE] = -1
    np.save(root / "wf_frames.npy", table)
    table.tofile(root / "wf_frames.raw")

    m = {
        "format_version": 0,
        "wfacq_version": "0.1.0+test",
        "session_id": "0123456789abcdef0123456789abcdef",
        "closed_cleanly": True,
        "close_error": None,
        "width": w,
        "height": h,
        "dtype": "uint16",
        "byte_order": "little",
        "frame_order": "C",
        "n_frames": n,
        "bin_bytes": n * h * w * 2,
        "frame_rate": 70.0,
        "pixel_format": "Mono12",
        "effective_bit_depth": 12,
        "pixel_shift_bits": 0,
        "subject": "TEST_0001",
        "session_date": "2026-10-12",
        "session_number": 1,
    }
    m.update(meta or {})
    (root / "wf_meta.json").write_text(json.dumps(m, indent=1), encoding="utf-8")
    (root / "wf_events.jsonl").write_text(
        json.dumps({"t": 1.0, "t_mono": 0.0, "type": "stall", "frame_counter": 3}) + "\n",
        encoding="utf-8",
    )
    if pfs:
        (root / "wf_camera.pfs").write_text("# Basler features file\nWidth\t48\n", encoding="utf-8")
    age(root, age_s)
    return root


def age(root, seconds):
    t = time.time() - seconds
    for p in root.iterdir():
        os.utime(p, (t, t))


def rewrite_meta(root, **changes):
    p = root / "wf_meta.json"
    m = json.loads(p.read_text(encoding="utf-8"))
    for k, v in changes.items():
        if v is _DROP:
            m.pop(k, None)
        else:
            m[k] = v
    p.write_text(json.dumps(m), encoding="utf-8")
    age(root, 7200)


_DROP = object()


@pytest.fixture
def session(tmp_path):
    return make_session(tmp_path / "TEST_0001" / "2026-10-12" / "1")


def snapshot(root):
    return {p.name: sha(p) for p in sorted(root.iterdir()) if p.is_file()}


# -- round trip -----------------------------------------------------------------------------------


def test_restore_rebuilds_wf_bin_and_every_session_file(session, tmp_path):
    before = snapshot(session)
    wfz = tmp_path / "out" / "widefield.wfz"
    wa.compress_session(session, wfz)
    out = tmp_path / "restored"
    r = wa.restore_session(wfz, out)
    assert snapshot(out) == before
    assert r["byte_identical"] is True
    assert snapshot(session) == before  # the source is untouched


def test_meta_records_source_and_is_version_3(session, tmp_path):
    wfz = tmp_path / "widefield.wfz"
    meta = wa.compress_session(session, wfz)
    bin_sha = sha(session / "wf.bin")
    stored = read_meta(wfz)
    for m in (meta, stored):
        assert m["format_version"] == 3
        assert m["source_kind"] == "wfacq_bin"
        assert m["source_bin_sha256"] == bin_sha
        assert m["pixels_sha256"] == bin_sha
        assert m["source_bin_bytes"] == (session / "wf.bin").stat().st_size
        assert m["shape"] == [40, 48]
        assert m["n_frames"] == 13
        assert m["shift"] == 0
        assert m["temporal_order_known"] is True
        # Nothing that gates deleting a tar may mistake this for a tar.
        assert m["source_tar_sha256"] is None
        assert m["wfacq"]["session_id"] == "0123456789abcdef0123456789abcdef"
        assert m["wfacq"]["format_version"] == 0
        assert set(m["session_files"]) == set(wa.SESSION_FILES)
        for name, rec in m["session_files"].items():
            assert rec["sha256"] == sha(session / name)


def test_session_without_pfs_is_accepted(tmp_path):
    # The synthetic source writes no .pfs (FORMAT.md, "Session folder": camera sessions only).
    s = make_session(tmp_path / "s", pfs=False)
    wfz = tmp_path / "a.wfz"
    meta = wa.compress_session(s, wfz)
    assert "wf_camera.pfs" not in meta["session_files"]
    out = tmp_path / "restored"
    wa.restore_session(wfz, out)
    assert snapshot(out) == snapshot(s)


def test_shift_stays_zero_even_when_every_value_has_zero_low_bits(tmp_path):
    # The tar path samples frames to choose a shift. Here the shift comes from metadata, so data
    # that happens to have zero low bits everywhere must still be stored unshifted.
    stack = (make_stack() & 0x0FF0).astype("<u2")  # every value a multiple of 16
    s = make_session(tmp_path / "s", stack=stack)
    wfz = tmp_path / "a.wfz"
    meta = wa.compress_session(s, wfz)
    assert meta["shift"] == 0
    assert meta["shift_could_have_been"] == 4
    wa.restore_session(wfz, tmp_path / "r")
    assert sha(tmp_path / "r" / "wf.bin") == sha(s / "wf.bin")


def test_verify_proves_byte_identity_and_writes_nothing(session, tmp_path):
    wfz = tmp_path / "out" / "widefield.wfz"
    wa.compress_session(session, wfz)
    before = snapshot(tmp_path / "out")
    for result in (wa.verify_session(wfz), verify(wfz)):  # codec.verify dispatches on the kind
        assert result["byte_identical"] is True
        assert result["bin_sha256"] == sha(session / "wf.bin")
        assert result["rebuilt_bytes"] == (session / "wf.bin").stat().st_size
        assert result["session_files_ok"] is True
    assert snapshot(tmp_path / "out") == before


def test_corrupt_codestream_fails_verify(session, tmp_path):
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)
    footer = container.read_footer(wfz)
    off = int(footer.index[3, 0]) + 20
    with open(wfz, "r+b") as fh:
        fh.seek(off)
        b = fh.read(1)
        fh.seek(off)
        fh.write(bytes([b[0] ^ 0x01]))
    with pytest.raises(codec.LosslessCheckFailed):
        wa.verify_session(wfz)


def test_restore_refuses_to_overwrite(session, tmp_path):
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)
    out = tmp_path / "restored"
    out.mkdir()
    (out / "wf.bin").write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        wa.restore_session(wfz, out)
    assert (out / "wf.bin").read_bytes() == b"precious"
    with pytest.raises(FileExistsError):
        wa.restore_session(wfz, session)  # never over the live session either
    assert not any(".partial-" in p.name for p in out.iterdir())


def test_output_refuses_to_replace_an_existing_wfz(session, tmp_path):
    wfz = tmp_path / "a.wfz"
    wfz.write_bytes(b"an older wfz")
    with pytest.raises(FileExistsError):
        wa.compress_session(session, wfz)
    assert wfz.read_bytes() == b"an older wfz"


# -- readers --------------------------------------------------------------------------------------


def test_reader_returns_the_frames_in_wf_bin_order(session, tmp_path):
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)
    mm = np.memmap(session / "wf.bin", dtype="<u2", mode="r", shape=(13, 40, 48))
    with WfzReader(wfz) as r:
        assert r.temporal_order_known
        assert r.n_frames == 13 and r.shape == (40, 48)
        for i in range(13):
            assert np.array_equal(r.frame(i), mm[i])
        assert r.member_name(2) == "wf.bin frame 2"
        assert r.session_file("wf_frames.raw") == (session / "wf_frames.raw").read_bytes()
        assert r.session_files() == sorted(wa.SESSION_FILES)


def test_extract_bin_reproduces_wf_bin(session, tmp_path):
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)
    out = tmp_path / "x" / "frames.bin"
    s = extract(wfz, out, fmt="bin")
    assert s["pixels_verified"] is True
    assert sha(out) == sha(session / "wf.bin")


def test_extract_files_mode_points_to_restore(session, tmp_path):
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)
    with pytest.raises(ValueError, match="restore"):
        extract(wfz, tmp_path / "frames", fmt="files")


def test_decompress_points_to_restore(session, tmp_path):
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)
    with pytest.raises(ValueError, match="restore"):
        codec.decompress(wfz, tmp_path / "out.tar")
    assert not (tmp_path / "out.tar").exists()


def test_iter_tar_bytes_refuses_instead_of_spinning(session, tmp_path):
    # An empty tar-header blob never yields a nonzero size, so the tar rebuild would loop forever.
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)
    with pytest.raises(ValueError):
        next(iter(codec.iter_tar_bytes(wfz)))


def test_reader_pinned_at_version_2_refuses_version_3(session, tmp_path, monkeypatch):
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)
    monkeypatch.setattr(container, "READER_FORMAT_VERSION", 2)
    with pytest.raises(ValueError, match="newer version"):
        read_meta(wfz)
    with pytest.raises(ValueError, match="newer version"):
        WfzReader(wfz)


def test_tar_output_is_still_version_2(tmp_path):
    from test_roundtrip import make_frames, write_raw_tar

    src = tmp_path / "in.tar"
    write_raw_tar(src, make_frames())
    meta = codec.compress(src, tmp_path / "a.wfz", shape=(40, 48))
    assert meta["format_version"] == 2
    assert "source_kind" not in meta
    assert read_meta(tmp_path / "a.wfz")["format_version"] == 2


def test_tar_deletion_gate_refuses_a_session_wfz(session, tmp_path):
    from wfcompress.lab import deletion

    wfz = session / "widefield.wfz"
    meta = wa.compress_session(session, wfz)
    record = {"ok": True, "tar": str(session / "wf.bin"), "wfz": str(wfz),
              "source_bytes": meta["source_bytes"], "output_bytes": meta["output_bytes"]}
    c = deletion.inspect(record)
    assert c.verdict != "SAFE"
    assert "source_tar_sha256" in c.reason


def test_matches_wfacq_load_session(session, tmp_path):
    wfacq = pytest.importorskip("wfacq")
    s = wfacq.load_session(session)
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)
    with WfzReader(wfz) as r:
        assert r.n_frames == s.n_frames
        assert r.shape == tuple(s.frames.shape[1:])
        for i in (0, INCOMPLETE, DARK, s.n_frames - 1):
            assert np.array_equal(r.frame(i), s.frames[i])
    assert wfacq.FRAME_DTYPE == RECORD


# -- refusals -------------------------------------------------------------------------------------


REFUSALS = {
    "not closed cleanly": dict(closed_cleanly=False),
    "closed_cleanly missing": dict(closed_cleanly=_DROP),
    "n_frames null": dict(n_frames=None),
    "no frames": dict(n_frames=0, bin_bytes=0),
    "bin_bytes null": dict(bin_bytes=None),
    "bin_bytes disagrees": dict(bin_bytes=13 * 40 * 48 * 2 + 2),
    "n_frames disagrees": dict(n_frames=12),
    "dtype": dict(dtype="uint8"),
    "byte order": dict(byte_order="big"),
    "frame order": dict(frame_order="F"),
    "pixel shift": dict(pixel_shift_bits=4),
    "height missing": dict(height=_DROP),
    "geometry disagrees": dict(height=48, width=40 + 1),
    "unknown wfAcq format": dict(format_version=2),
}


@pytest.mark.parametrize("why", sorted(REFUSALS))
def test_refuses_metadata_problems(session, tmp_path, why):
    rewrite_meta(session, **REFUSALS[why])
    wfz = tmp_path / "a.wfz"
    with pytest.raises(wa.SessionRefused):
        wa.compress_session(session, wfz)
    assert not wfz.exists()
    assert not list(tmp_path.glob("*.partial-*"))


def test_refuses_when_meta_tmp_exists(session, tmp_path):
    (session / "wf_meta.json.tmp").write_text("{}", encoding="utf-8")
    age(session, 7200)
    with pytest.raises(wa.SessionRefused, match="tmp"):
        wa.compress_session(session, tmp_path / "a.wfz")


@pytest.mark.parametrize("missing", ["wf_meta.json", "wf.bin", "wf_frames.npy", "wf_frames.raw",
                                     "wf_events.jsonl"])
def test_refuses_missing_files(session, tmp_path, missing):
    (session / missing).unlink()
    with pytest.raises(wa.SessionRefused):
        wa.compress_session(session, tmp_path / "a.wfz")


def test_refuses_truncated_wf_bin(session, tmp_path):
    p = session / "wf.bin"
    data = p.read_bytes()
    p.write_bytes(data[:-100])  # a copy that has not finished arriving
    age(session, 7200)
    with pytest.raises(wa.SessionRefused, match="wf.bin"):
        wa.compress_session(session, tmp_path / "a.wfz")


def test_refuses_short_raw_table(session, tmp_path):
    p = session / "wf_frames.raw"
    p.write_bytes(p.read_bytes()[:-30])
    age(session, 7200)
    with pytest.raises(wa.SessionRefused, match="wf_frames.raw"):
        wa.compress_session(session, tmp_path / "a.wfz")


def test_refuses_npy_table_of_wrong_length(session, tmp_path):
    t = np.load(session / "wf_frames.npy")
    np.save(session / "wf_frames.npy", t[:-1])
    age(session, 7200)
    with pytest.raises(wa.SessionRefused, match="wf_frames.npy"):
        wa.compress_session(session, tmp_path / "a.wfz")


def test_refuses_npy_and_raw_that_disagree(session, tmp_path):
    t = np.load(session / "wf_frames.npy")
    t["flags"][0] = 1
    np.save(session / "wf_frames.npy", t)
    age(session, 7200)
    with pytest.raises(wa.SessionRefused, match="disagree"):
        wa.compress_session(session, tmp_path / "a.wfz")


def test_refuses_npy_with_wrong_record_layout(session, tmp_path):
    np.save(session / "wf_frames.npy", np.zeros(13, dtype="<u8"))
    age(session, 7200)
    with pytest.raises(wa.SessionRefused, match="wf_frames.npy"):
        wa.compress_session(session, tmp_path / "a.wfz")


@pytest.mark.parametrize("name", ["wf.bin", "wf_events.jsonl", "wf_camera.pfs"])
def test_refuses_recently_modified_sessions(session, tmp_path, name):
    os.utime(session / name, None)  # touched just now
    with pytest.raises(wa.SessionRefused, match="min"):
        wa.compress_session(session, tmp_path / "a.wfz")
    # and the rule can be turned off explicitly
    wa.compress_session(session, tmp_path / "a.wfz", min_age_s=0)


def test_detects_a_session_file_changing_mid_run(session, tmp_path):
    wfz = tmp_path / "a.wfz"
    calls = []

    def progress(done, total, elapsed):
        if not calls:
            with open(session / "wf_events.jsonl", "a", encoding="utf-8") as fh:
                fh.write('{"t": 2.0, "t_mono": 1.0, "type": "health"}\n')
        calls.append(done)

    with pytest.raises(codec.SourceChanged):
        wa.compress_session(session, wfz, batch=4, progress=progress)
    assert calls  # the change really happened during the run
    assert not wfz.exists()
    assert not list(tmp_path.glob("*.partial-*"))


# -- the one-call path and the CLI ----------------------------------------------------------------


def test_convert_writes_wfz_and_verified_receipt_beside_the_session(session):
    before = snapshot(session)
    r = wa.convert_session(session)
    wfz = session / "widefield.wfz"
    assert r["wfz"] == str(wfz) and wfz.is_file()
    receipt = json.loads((session / "widefield.wfz.receipt.json").read_text(encoding="utf-8"))
    assert receipt["byte_identical"] is True
    assert receipt["verified_by"] == "stream"
    assert receipt["source_bin_sha256"] == sha(session / "wf.bin")
    assert receipt["byte_identical_verified"] is True
    assert receipt["source_tar_sha256"] is None
    readme = (session / "widefield.wfz.README.md").read_text(encoding="utf-8")
    assert "wf.bin" in readme and "restore" in readme
    after = snapshot(session)
    for name, digest in before.items():  # every original file is untouched
        assert after[name] == digest


def test_cli_compress_check_restore(session, tmp_path, capsys):
    wfz = tmp_path / "widefield.wfz"
    assert cli.main(["--quiet", "compress", str(session), str(wfz)]) == 0
    assert (tmp_path / "widefield.wfz.receipt.json").is_file()
    assert cli.main(["--quiet", "check", str(wfz)]) == 0
    assert "BYTE-IDENTICAL" in capsys.readouterr().out
    assert cli.main(["--quiet", "restore", str(wfz), str(tmp_path / "r")]) == 0
    assert snapshot(tmp_path / "r") == snapshot(session)
    assert cli.main(["--quiet", "peek", str(wfz), "--frame", "1"]) == 0


# -- review findings ------------------------------------------------------------------------------


def rewrite_footer_meta(wfz, **changes):
    """Edit meta.json inside a .wfz's footer, keeping everything else. Simulates corruption."""
    import io
    import struct
    import zipfile

    with open(wfz, "rb") as fh:
        fh.seek(8)
        offset = struct.unpack("<Q", fh.read(8))[0]
        fh.seek(offset)
        old = zipfile.ZipFile(io.BytesIO(fh.read()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in old.namelist():
            data = old.read(name)
            if name == "meta.json":
                m = json.loads(data)
                for k, v in changes.items():
                    if callable(v):
                        v(m)
                    else:
                        m[k] = v
                data = json.dumps(m).encode()
            z.writestr(name, data)
    with open(wfz, "r+b") as fh:
        fh.seek(offset)
        fh.write(buf.getvalue())
        fh.truncate()


@pytest.mark.parametrize("batch", [1, 4, 64])
def test_verify_and_restore_with_partial_last_batch(session, tmp_path, batch):
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz, batch=batch)
    assert wa.verify_session(wfz, batch=batch)["byte_identical"] is True
    wa.restore_session(wfz, tmp_path / "r", batch=batch)
    assert sha(tmp_path / "r" / "wf.bin") == sha(session / "wf.bin")


def test_compress_refuses_a_stale_receipt(session):
    # An old receipt beside a new, unverified .wfz would vouch for it.
    wa.convert_session(session)
    (session / "widefield.wfz").unlink()
    with pytest.raises(FileExistsError, match="receipt"):
        wa.compress_session(session, session / "widefield.wfz")
    assert not (session / "widefield.wfz").exists()


def test_compress_never_replaces_output_that_appears_mid_run(session, tmp_path):
    wfz = tmp_path / "a.wfz"

    def progress(done, total, elapsed):
        if not wfz.exists():
            wfz.write_bytes(b"another run's output")

    with pytest.raises(FileExistsError):
        wa.compress_session(session, wfz, batch=4, progress=progress)
    assert wfz.read_bytes() == b"another run's output"
    assert not list(tmp_path.glob("*.partial-*"))


def test_convert_never_replaces_a_receipt_that_appears_mid_run(session, tmp_path):
    # The start check cannot see a receipt written during the minutes of compress and verify.
    wfz = tmp_path / "a.wfz"
    receipt = tmp_path / "a.wfz.receipt.json"

    def verify_progress(done, total, elapsed):
        if not receipt.exists():
            receipt.write_text("another run's receipt", encoding="utf-8")

    with pytest.raises(FileExistsError):
        wa.convert_session(session, wfz, batch=4, verify_progress=verify_progress)
    assert receipt.read_text(encoding="utf-8") == "another run's receipt"
    assert not list(tmp_path.glob("*.partial-*"))


def test_restore_never_replaces_a_file_that_appears_mid_run(session, tmp_path):
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)
    out = tmp_path / "r"

    def progress(done, total, elapsed):
        out.mkdir(exist_ok=True)
        if not (out / "wf.bin").exists():
            (out / "wf.bin").write_bytes(b"precious")

    with pytest.raises(FileExistsError):
        wa.restore_session(wfz, out, batch=4, progress=progress)
    assert (out / "wf.bin").read_bytes() == b"precious"
    assert sorted(p.name for p in out.iterdir()) == ["wf.bin"]  # nothing half restored


def test_restore_bin_only_into_the_session_folder(session, tmp_path):
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)
    original = sha(session / "wf.bin")
    (session / "wf.bin").rename(tmp_path / "wf.bin.moved")
    r = wa.restore_session(wfz, session, only_bin=True)
    assert r["files"] == ["wf.bin"]
    assert sha(session / "wf.bin") == original


@pytest.mark.parametrize("tamper", [
    dict(session_files=lambda m: m["session_files"].pop("wf_meta.json")),
    dict(session_files=lambda m: m["session_files"].pop("wf_frames.raw")),
    dict(source_bin_bytes=1, source_bytes=1),
    dict(shape=[48, 40]),
    dict(wfacq=lambda m: m["wfacq"].__setitem__("session_id", "f" * 32)),
])
def test_verify_cross_checks_the_stored_metadata(session, tmp_path, tamper):
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)
    rewrite_footer_meta(wfz, **tamper)
    with pytest.raises((codec.LosslessCheckFailed, KeyError)):
        wa.verify_session(wfz)


def test_restore_refuses_unsafe_stored_names(session, tmp_path):
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)

    def evil(m):
        m["session_files"]["../escape.txt"] = m["session_files"]["wf_events.jsonl"]

    rewrite_footer_meta(wfz, session_files=evil)
    with pytest.raises(codec.LosslessCheckFailed):
        wa.restore_session(wfz, tmp_path / "r")
    assert not (tmp_path / "escape.txt").exists()


def test_camera_session_without_pfs_is_accepted(tmp_path):
    # wfAcq writes wf_camera.pfs only if reading the camera's features succeeded, so a cleanly
    # closed camera session can legitimately lack it.
    s = make_session(tmp_path / "s", pfs=False,
                     meta={"camera": {"source": "pylon", "model": "acA2440-75um"}})
    meta = wa.compress_session(s, tmp_path / "a.wfz")
    assert "wf_camera.pfs" not in meta["session_files"]


def test_refuses_robocopy_incomplete_timestamp(session, tmp_path):
    # robocopy stamps a file it has not finished copying 1980-01-01, at its full size.
    t = 315532800 + 86400  # 1980-01-02 UTC
    os.utime(session / "wf.bin", (t, t))
    with pytest.raises(wa.SessionRefused, match="1980"):
        wa.compress_session(session, tmp_path / "a.wfz")


def test_stored_session_files_are_the_bytes_that_were_validated(session, tmp_path, monkeypatch):
    # Files are read once; what is checked is what is stored.
    reads = []
    real = wa._read_session_files

    def counting(path, names):
        reads.append(tuple(names))
        return real(path, names)

    monkeypatch.setattr(wa, "_read_session_files", counting)
    wa.compress_session(session, tmp_path / "a.wfz")
    assert len(reads) == 1


def test_cli_session_defaults_to_widefield_wfz_and_refuses_no_sidecar(session, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--quiet", "compress", str(session), "--no-sidecar"])
    assert not (session / "widefield.wfz").exists()
    assert cli.main(["--quiet", "compress", str(session)]) == 0
    assert (session / "widefield.wfz").is_file()
    assert cli.main(["--quiet", "check", str(session / "widefield.wfz")]) == 0
    assert sha(session / "wf.bin") in capsys.readouterr().out  # the full hash, comparable


def test_bin_only_refuses_a_folder_of_a_different_session(tmp_path):
    a = make_session(tmp_path / "a")
    b = make_session(tmp_path / "b", meta={"session_id": "f" * 32})
    (b / "wf.bin").unlink()
    wfz = tmp_path / "a.wfz"
    wa.compress_session(a, wfz)
    with pytest.raises(codec.LosslessCheckFailed, match="wf_meta.json"):
        wa.restore_session(wfz, b, only_bin=True)
    assert not (b / "wf.bin").exists()


def test_bin_only_refuses_a_folder_without_metadata(session, tmp_path):
    wfz = tmp_path / "a.wfz"
    wa.compress_session(session, wfz)
    with pytest.raises(codec.LosslessCheckFailed, match="wf_meta.json"):
        wa.restore_session(wfz, tmp_path / "empty", only_bin=True)
    assert not (tmp_path / "empty" / "wf.bin").exists()


def test_receipt_and_verify_identify_the_wfz_itself(session):
    r = wa.convert_session(session)
    wfz = session / "widefield.wfz"
    receipt = json.loads((session / "widefield.wfz.receipt.json").read_text(encoding="utf-8"))
    assert receipt["wfz_sha256"] == sha(wfz)
    assert receipt["output_bytes"] == wfz.stat().st_size
    assert wa.verify_session(wfz)["wfz_sha256"] == sha(wfz)
    assert r["wfz_sha256"] == sha(wfz)


def test_no_clobber_commit_without_rename_semantics(tmp_path, monkeypatch):
    # The non-Windows branch: a hard link refuses an existing target.
    monkeypatch.setattr(codec, "_RENAME_REFUSES_EXISTING", False)
    tmp, dst = tmp_path / "t", tmp_path / "d"
    tmp.write_bytes(b"new")
    codec._commit_no_clobber(tmp, dst)
    assert dst.read_bytes() == b"new" and not tmp.exists()
    tmp.write_bytes(b"newer")
    with pytest.raises(FileExistsError):
        codec._commit_no_clobber(tmp, dst)
    assert dst.read_bytes() == b"new"


def test_no_clobber_commit_where_hard_links_are_unsupported(tmp_path, monkeypatch):
    # Many SMB mounts refuse hard links; the fallback reserves the name exclusively first.
    monkeypatch.setattr(codec, "_RENAME_REFUSES_EXISTING", False)

    def no_links(*a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(codec.os, "link", no_links)
    tmp, dst = tmp_path / "t", tmp_path / "d"
    tmp.write_bytes(b"new")
    codec._commit_no_clobber(tmp, dst)
    assert dst.read_bytes() == b"new" and not tmp.exists()
    tmp.write_bytes(b"newer")
    with pytest.raises(FileExistsError):
        codec._commit_no_clobber(tmp, dst)
    assert dst.read_bytes() == b"new"
