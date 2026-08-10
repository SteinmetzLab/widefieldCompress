"""Dropping non-frame members: only with evidence, and only the frames come back.

These archives are the ones that bundle a whole SpikeGLX recording behind the widefield frames.
Keeping just the frames is the first operation here that destroys information, so the tests are
mostly about what it *refuses* to do.
"""

from __future__ import annotations

import io
import tarfile

import numpy as np
import pytest
from test_roundtrip import make_frames, write_raw_tar

from wfcompress import compress, decompress, read_meta, sha256_file, verify
from wfcompress.codec import UnsupportedArchive, UnverifiedDiscard
from wfcompress.lab import mixed


def write_mixed_tar(path, frames, extras: dict[str, bytes], lead_dir=True):
    """Frames first, then arbitrary other members - the real archives' shape."""
    with tarfile.open(path, "w") as tf:
        if lead_dir:
            d = tarfile.TarInfo("1/")
            d.type = tarfile.DIRTYPE
            tf.addfile(d)
        for i, f in enumerate(frames):
            data = f.astype("<u2").tobytes()
            info = tarfile.TarInfo(f"1/frame-{i}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for name, blob in extras.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))


def evidence(names_to_blobs, outside_dir, verified=True):
    return {
        name: {"member": name, "member_bytes": len(blob), "verified": verified,
               "method": "sha256", "outside_path": str(outside_dir / name.split("/")[-1]),
               "sha256": "deadbeef"}
        for name, blob in names_to_blobs.items()
    }


@pytest.fixture
def mixed_tar(tmp_path):
    frames = make_frames(n=12, rows=32, cols=50)
    extras = {
        "1/p0.missed_samples.imec0.txt": b"x" * 3919,
        "1/p0_g0/p0_g0_t0.imec0.ap.bin": bytes(range(256)) * 4096,
        "1/p0_g0/p0_g0_t0.imec0.ap.meta": b"nSavedChans=385\nimSampRate=30000\n",
    }
    src = tmp_path / "widefield.tar"
    write_mixed_tar(src, frames, extras)
    return src, frames, extras


def test_mixed_archive_is_refused_without_a_manifest(mixed_tar, tmp_path):
    src, _frames, _extras = mixed_tar
    with pytest.raises(UnsupportedArchive, match="different sizes"):
        compress(src, tmp_path / "a.wfz", shape=(32, 50))
    assert not (tmp_path / "a.wfz").exists()


def test_dropping_without_evidence_is_refused(mixed_tar, tmp_path):
    src, _frames, extras = mixed_tar
    partial_manifest = evidence({k: v for k, v in list(extras.items())[:1]}, tmp_path)
    with pytest.raises(UnverifiedDiscard, match="no verified copy"):
        compress(src, tmp_path / "a.wfz", shape=(32, 50), drop_members=partial_manifest)
    assert not (tmp_path / "a.wfz").exists()
    assert not list(tmp_path.glob("*.partial-*"))


def test_evidence_marked_unverified_is_refused(mixed_tar, tmp_path):
    src, _frames, extras = mixed_tar
    with pytest.raises(UnverifiedDiscard):
        compress(src, tmp_path / "a.wfz", shape=(32, 50),
                 drop_members=evidence(extras, tmp_path, verified=False))


def test_frames_only_rebuild_contains_exactly_the_frames(mixed_tar, tmp_path):
    src, frames, extras = mixed_tar
    wfz = tmp_path / "a.wfz"
    meta = compress(src, wfz, shape=(32, 50), drop_members=evidence(extras, tmp_path))

    assert meta["partial"] is True
    assert meta["n_frames"] == len(frames)
    assert meta["source_tar_sha256"] is None, "must not claim to rebuild its input"
    assert meta["frames_tar_sha256"]
    assert meta["dropped_bytes"] == sum(len(v) for v in extras.values())
    assert {d["member"] for d in meta["dropped_members"]} == set(extras)

    out = tmp_path / "frames.tar"
    decompress(wfz, out)
    with tarfile.open(out) as tf:
        names = tf.getnames()
        got = {n: np.frombuffer(tf.extractfile(n).read(), "<u2").reshape(32, 50)
               for n in names if n.startswith("1/frame-")}
    assert not any("imec0" in n for n in names), "ephys members leaked into the rebuild"
    assert not any("missed_samples" in n for n in names)
    assert len(got) == len(frames)
    for name, arr in got.items():
        np.testing.assert_array_equal(arr, frames[int(name.rsplit("-", 1)[1])])


def test_partial_archive_verifies_against_its_own_rebuild(mixed_tar, tmp_path):
    src, _frames, extras = mixed_tar
    wfz = tmp_path / "a.wfz"
    compress(src, wfz, shape=(32, 50), drop_members=evidence(extras, tmp_path))
    r = verify(wfz)
    assert r["partial"] is True
    assert r["byte_identical"] is True, "must reproduce the frames-only tar exactly"
    assert r["size_matches"]


def test_rebuilt_frames_tar_is_a_valid_tar(mixed_tar, tmp_path):
    """The synthesised trailer has to satisfy a real tar reader, not just our own."""
    src, _frames, extras = mixed_tar
    wfz = tmp_path / "a.wfz"
    compress(src, wfz, shape=(32, 50), drop_members=evidence(extras, tmp_path))
    out = tmp_path / "frames.tar"
    decompress(wfz, out)
    assert out.stat().st_size % (20 * 512) == 0, "tar blocking factor not respected"
    with tarfile.open(out) as tf:
        for m in tf.getmembers():
            if m.isfile():
                assert len(tf.extractfile(m).read()) == m.size


def test_no_read_span_covers_a_dropped_member(mixed_tar, tmp_path):
    """Spans are read as one contiguous byte range. On a real archive the dropped member is 82 GB,
    so a span straddling it would pull the whole ephys recording through memory to reach the next
    frame. Check the ranges directly rather than inferring it from timing."""
    from wfcompress import tarwalk
    from wfcompress.codec import _entry_spans, _partition

    src, _frames, extras = mixed_tar
    entries = tarwalk.read_entries(src)
    kept, dropped, _ = _partition(entries, evidence(extras, tmp_path))
    assert dropped, "fixture produced nothing to drop"

    for span_start, span_end, _ in _entry_spans(kept, batch=64):
        lo = kept[span_start].data_offset - tarwalk.BLOCK
        hi = kept[span_end - 1].end_offset
        for d in dropped:
            assert not (lo < d.end_offset and d.data_offset < hi), (
                f"span [{lo}, {hi}) covers dropped member {d.name}"
            )


def test_partition_picks_the_modal_size(tmp_path):
    from wfcompress import tarwalk

    frames = make_frames(n=9, rows=32, cols=50)
    src = tmp_path / "m.tar"
    write_mixed_tar(src, frames, {"1/big.bin": b"z" * 99_991, "1/small.txt": b"hi"})
    entries = tarwalk.read_entries(src)
    frame_size, kept, others = mixed.partition(entries)
    assert frame_size == 32 * 50 * 2
    assert len(kept) == 9
    assert sorted(e.name for e in others) == ["1/big.bin", "1/small.txt"]


def test_uniform_archive_is_untouched_by_the_new_code_path(tmp_path):
    """The default must still be a byte-identical rebuild of the input."""
    frames = make_frames(n=10, rows=32, cols=50)
    src = tmp_path / "in.tar"
    write_raw_tar(src, frames)
    wfz, out = tmp_path / "a.wfz", tmp_path / "out.tar"
    meta = compress(src, wfz, shape=(32, 50))
    assert meta["partial"] is False
    assert meta["source_tar_sha256"] == sha256_file(src)
    decompress(wfz, out)
    assert sha256_file(out) == sha256_file(src)
    assert read_meta(wfz)["dropped_bytes"] == 0
