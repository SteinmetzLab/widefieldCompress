"""Regression tests for the findings of the 2026-08-03 code review.

Each of these reproduces a defect that was confirmed against real tars before being fixed.
"""

from __future__ import annotations

import io
import json
import struct
import tarfile
import zipfile

import numpy as np
import pytest
import tifffile

from wfcompress import (
    GeometryUnknown,
    WfzReader,
    codec,
    compress,
    decompress,
    read_meta,
    sha256_file,
    verify,
)
from wfcompress.codec import SourceChanged, UnsupportedArchive


def make_frames(n=12, rows=40, cols=48, shift=0, seed=0):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:rows, 0:cols]
    base = (300 + 120 * np.sin(x / 7) * np.cos(y / 5)).astype(np.int32)
    return np.stack(
        [
            np.clip(base + i + rng.integers(0, 12, (rows, cols)), 0, 4095).astype(np.uint16)
            << shift
            for i in range(n)
        ]
    )


def write_tiff_tar(path, frames):
    with tarfile.open(path, "w") as tf:
        for i, f in enumerate(frames):
            buf = io.BytesIO()
            tifffile.imwrite(buf, f.astype(">u2"), photometric="minisblack")
            data = buf.getvalue()
            info = tarfile.TarInfo(f"1/frame_{i:05d}.tiff")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def write_raw_tar(path, frames, lexicographic=False):
    names = (
        sorted(f"frame-{i}" for i in range(len(frames)))
        if lexicographic
        else [f"frame-{i}" for i in range(len(frames))]
    )
    with tarfile.open(path, "w") as tf:
        for name in names:
            i = int(name.split("-")[1])
            data = frames[i].astype("<u2").tobytes()
            info = tarfile.TarInfo(f"1/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


# --- P0: destroying your own input ------------------------------------------------------------


def test_compress_refuses_to_overwrite_its_own_source(tmp_path):
    """Measured before the guard: a 10,240-byte source came back as 16 bytes."""
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames(n=3))
    before = src.read_bytes()
    with pytest.raises(ValueError, match="same"):
        compress(src, src)
    assert src.read_bytes() == before


def test_decompress_refuses_to_overwrite_its_own_source(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames(n=3))
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)
    before = wfz.read_bytes()
    with pytest.raises(ValueError, match="same"):
        decompress(wfz, wfz)
    assert wfz.read_bytes() == before


def test_failed_compression_leaves_no_output_and_no_temp_file(tmp_path):
    src = tmp_path / "in.tar"
    write_raw_tar(src, make_frames(n=4, rows=32, cols=50))
    dst = tmp_path / "a.wfz"
    with pytest.raises(GeometryUnknown):
        compress(src, dst)
    assert not dst.exists()
    assert not list(tmp_path.glob("*.partial-*"))


def test_existing_destination_is_replaced_atomically(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames(n=4))
    dst = tmp_path / "a.wfz"
    dst.write_bytes(b"stale content that must not survive")
    compress(src, dst)
    assert verify(dst)["byte_identical"] is True
    assert not list(tmp_path.glob("*.partial-*"))


# --- P1: tar layouts the byte-identical claim did not cover -------------------------------------


def test_trailing_zero_size_entry_is_refused(tmp_path):
    """A directory entry after the last frame is never emitted by the reconstruction loop, so the
    rebuilt archive had a different SHA-256."""
    src = tmp_path / "in.tar"
    with tarfile.open(src, "w") as tf:
        for i, f in enumerate(make_frames(n=4)):
            buf = io.BytesIO()
            tifffile.imwrite(buf, f, photometric="minisblack")
            data = buf.getvalue()
            info = tarfile.TarInfo(f"1/f{i}.tiff")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        d = tarfile.TarInfo("1/after/")
        d.type = tarfile.DIRTYPE
        tf.addfile(d)
    with pytest.raises(UnsupportedArchive, match="after the last data member"):
        compress(src, tmp_path / "a.wfz")


def test_nonzero_member_padding_is_refused(tmp_path):
    """Reconstruction synthesises zero padding, so nonzero padding would not round-trip."""
    src = tmp_path / "in.tar"
    # 32x32 leaves the TIFF at a size that is not 512-aligned, so there is padding to dirty
    write_tiff_tar(src, make_frames(n=3, rows=32, cols=32))
    with tarfile.open(src) as tf:
        m = tf.getmembers()[0]
        pad_at, pad_len = m.offset_data + m.size, (-m.size) % 512
    assert pad_len, "test needs a member whose size is not a multiple of 512"
    raw = bytearray(src.read_bytes())
    raw[pad_at] = 0xAB
    src.write_bytes(bytes(raw))
    with pytest.raises(UnsupportedArchive, match="padding"):
        compress(src, tmp_path / "a.wfz")


# --- P1: acquisition order vs storage order -----------------------------------------------------


def test_reader_returns_acquisition_order_not_storage_order(tmp_path):
    """These tars are written lexicographically, so storage slot 2 holds frame-10. frame(2) must
    still return acquisition frame 2."""
    fs = make_frames(n=12, rows=32, cols=32)
    src = tmp_path / "in.tar"
    write_raw_tar(src, fs, lexicographic=True)

    wfz = tmp_path / "a.wfz"
    compress(src, wfz, shape=(32, 32))
    with WfzReader(wfz) as r:
        assert r.temporal_order_known
        assert r.member_name(2) == "1/frame-2"
        for i in range(12):
            np.testing.assert_array_equal(r.frame(i), fs[i])
        np.testing.assert_array_equal(r.frame_by_storage_index(2), fs[10])

    out = tmp_path / "out.tar"
    decompress(wfz, out)
    assert sha256_file(src) == sha256_file(out), "storage order must survive the rebuild"


def test_temporal_order_absent_is_reported_not_assumed(tmp_path):
    fs = make_frames(n=5, rows=32, cols=32)
    src = tmp_path / "in.tar"
    with tarfile.open(src, "w") as tf:
        for i, name in enumerate(["alpha", "bravo", "charlie", "delta", "echo"]):
            data = fs[i].astype("<u2").tobytes()
            info = tarfile.TarInfo(f"1/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    wfz = tmp_path / "a.wfz"
    meta = compress(src, wfz, shape=(32, 32))
    assert meta["temporal_order_known"] is False
    with WfzReader(wfz) as r:
        assert not r.temporal_order_known
        np.testing.assert_array_equal(r.frame(0), fs[0])


# --- P1/P2: honest metadata ---------------------------------------------------------------------


def test_compress_does_not_claim_byte_identity_until_verified(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames())
    wfz = tmp_path / "a.wfz"
    meta = compress(src, wfz)
    assert meta["byte_identical_verified"] is False
    assert verify(wfz)["byte_identical"] is True


def test_payload_bits_reflect_every_frame_not_the_sample(tmp_path):
    frames = make_frames(n=20, shift=4)
    frames[17] = frames[17] | (1 << 15)  # a bright value only in one late frame
    src = tmp_path / "in.tar"
    write_tiff_tar(src, frames)
    meta = compress(src, tmp_path / "a.wfz")
    assert meta["observed_or_mask"] & (1 << 15)
    assert meta["payload_bits"] >= 12


def test_decompress_checks_the_whole_tar_hash(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames())
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)
    result = decompress(wfz, tmp_path / "out.tar")
    assert result["byte_identical"] is True
    assert result["tar_sha256"] == sha256_file(src)


# --- P1: a source that changes underneath us ----------------------------------------------------


def test_source_changing_during_compression_is_detected(tmp_path, monkeypatch):
    """A same-size in-place write must not be baked into a self-consistent snapshot of a state the
    archive never actually had."""
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames(n=6))
    real = codec._source_fingerprint
    calls = {"n": 0}

    def drifting(path):
        calls["n"] += 1
        size, mtime = real(path)
        return (size, mtime + 1) if calls["n"] > 1 else (size, mtime)

    monkeypatch.setattr(codec, "_source_fingerprint", drifting)
    with pytest.raises(SourceChanged):
        compress(src, tmp_path / "a.wfz")
    assert not (tmp_path / "a.wfz").exists()


def test_min_age_rejects_a_freshly_written_archive(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames(n=3))
    with pytest.raises(SourceChanged, match="still be being written"):
        compress(src, tmp_path / "a.wfz", min_age_s=3600)


# --- P2: format longevity -----------------------------------------------------------------------


def test_reader_refuses_a_future_format_version(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames(n=3))
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)

    blob = bytearray(wfz.read_bytes())
    offset = struct.unpack("<Q", bytes(blob[8:16]))[0]
    z = zipfile.ZipFile(io.BytesIO(bytes(blob[offset:])))
    items = {n: z.read(n) for n in z.namelist()}
    meta = json.loads(items["meta.json"])
    meta["format_version"] = 99
    items["meta.json"] = json.dumps(meta).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zz:
        for n, d in items.items():
            zz.writestr(n, d)
    wfz.write_bytes(bytes(blob[:offset]) + out.getvalue())

    with pytest.raises(ValueError, match="format version 99"):
        read_meta(wfz)


# --- P3: corruption tests should be specific ----------------------------------------------------


def test_corrupted_codestream_raises_the_integrity_error_not_just_anything(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames())
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)
    blob = bytearray(wfz.read_bytes())
    blob[300] ^= 0xFF
    wfz.write_bytes(bytes(blob))

    out = tmp_path / "out.tar"
    with pytest.raises(codec.LosslessCheckFailed, match="CRC"):
        decompress(wfz, out)
    assert not out.exists(), "a failed decompression must not leave an output file"
