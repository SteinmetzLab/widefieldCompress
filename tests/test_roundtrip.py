"""Round-trip tests on synthetic archives. No server, no real data needed."""

from __future__ import annotations

import io
import tarfile

import numpy as np
import pytest
import tifffile

from wfcompress import GeometryUnknown, WfzReader, compress, decompress, read_meta, sha256_file


def make_frames(n=12, rows=40, cols=48, shift=0, seed=0):
    """Smooth-ish 12-bit images so JPEG-LS has something realistic to predict."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:rows, 0:cols]
    base = (300 + 120 * np.sin(x / 7) * np.cos(y / 5)).astype(np.int32)
    out = []
    for i in range(n):
        f = base + i + rng.integers(0, 12, size=(rows, cols))
        out.append((np.clip(f, 0, 4095).astype(np.uint16) << shift))
    return np.stack(out)


def write_tiff_tar(path, frames, byteorder=">"):
    with tarfile.open(path, "w") as tf:
        for i, f in enumerate(frames):
            buf = io.BytesIO()
            tifffile.imwrite(buf, f.astype(byteorder + "u2"), photometric="minisblack")
            data = buf.getvalue()
            info = tarfile.TarInfo(f"1/frame_{i:05d}.tiff")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def write_raw_tar(path, frames):
    with tarfile.open(path, "w") as tf:
        for i, f in enumerate(frames):
            data = f.astype("<u2").tobytes()
            info = tarfile.TarInfo(f"1/frame-{i}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


@pytest.mark.parametrize("shift", [0, 4])
def test_tiff_tar_roundtrips_byte_identically(tmp_path, shift):
    frames = make_frames(shift=shift)
    src = tmp_path / "in.tar"
    write_tiff_tar(src, frames)

    wfz, out = tmp_path / "a.wfz", tmp_path / "out.tar"
    meta = compress(src, wfz)
    assert meta["shift"] == shift
    assert meta["is_tiff"]
    assert meta["lossless"] is True

    decompress(wfz, out)
    assert sha256_file(src) == sha256_file(out)
    assert out.stat().st_size == src.stat().st_size


def test_raw_tar_roundtrips_byte_identically(tmp_path):
    frames = make_frames(rows=32, cols=50)  # deliberately non-square
    src = tmp_path / "in.tar"
    write_raw_tar(src, frames)

    wfz, out = tmp_path / "a.wfz", tmp_path / "out.tar"
    compress(src, wfz, shape=(32, 50))
    decompress(wfz, out)
    assert sha256_file(src) == sha256_file(out)


def test_headerless_without_shape_refuses_rather_than_guessing(tmp_path):
    src = tmp_path / "in.tar"
    write_raw_tar(src, make_frames(rows=32, cols=50))
    with pytest.raises(GeometryUnknown):
        compress(src, tmp_path / "a.wfz")


def test_size_incompatible_shape_is_rejected(tmp_path):
    src = tmp_path / "in.tar"
    write_raw_tar(src, make_frames(rows=32, cols=50))
    with pytest.raises(GeometryUnknown):
        compress(src, tmp_path / "a.wfz", shape=(40, 41))


def test_size_compatible_wrong_shape_still_restores_byte_identically(tmp_path):
    """A wrong-but-same-size shape cannot be detected from the archive alone -- 32x50 and 40x40
    are both 1600 pixels. It is not a data-integrity risk: the pixels are stored and returned in
    file order either way, so the tar still rebuilds byte-for-byte. What it costs is compression
    (the predictor sees scrambled rows) and a wrongly-shaped array out of WfzReader.frame().
    """
    frames = make_frames(rows=32, cols=50)
    src = tmp_path / "in.tar"
    write_raw_tar(src, frames)

    right, wrong = tmp_path / "right.wfz", tmp_path / "wrong.wfz"
    m_right = compress(src, right, shape=(32, 50))
    m_wrong = compress(src, wrong, shape=(40, 40))

    for wfz in (right, wrong):
        out = tmp_path / f"{wfz.stem}.tar"
        decompress(wfz, out)
        assert sha256_file(src) == sha256_file(out), "restore must be exact regardless of shape"

    # the only observable penalty is a worse ratio, which is why the shape should come from an
    # authoritative source rather than being guessed
    assert m_wrong["output_bytes"] >= m_right["output_bytes"]


def test_reader_returns_original_pixels(tmp_path):
    frames = make_frames(shift=4)
    src = tmp_path / "in.tar"
    write_tiff_tar(src, frames)
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)

    with WfzReader(wfz) as r:
        assert r.n_frames == len(frames)
        assert r.shape == frames.shape[1:]
        for i in (0, 5, len(frames) - 1):
            np.testing.assert_array_equal(r.frame(i), frames[i])
        np.testing.assert_array_equal(r[0:3], frames[0:3])
        assert r.member_name(0) == "1/frame_00000.tiff"


def test_compression_actually_helps(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames(n=40))
    wfz = tmp_path / "a.wfz"
    meta = compress(src, wfz)
    assert meta["ratio"] > 1.5


def test_metadata_is_self_describing(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames(shift=4))
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)
    meta = read_meta(wfz)
    assert meta["codec"] == "jpegls"
    assert meta["near"] == 0
    assert "github.com/SteinmetzLab/widefieldCompress" in meta["how_to_decompress"]
    assert meta["provenance"]["tool"] == "wfcompress"
    assert meta["shift"] == 4


def _tar_with_varying_headers(path, frames, descriptions):
    """TIFFs whose metadata differs per frame, so the shells are not all identical."""
    with tarfile.open(path, "w") as tf:
        for i, (f, desc) in enumerate(zip(frames, descriptions)):
            buf = io.BytesIO()
            tifffile.imwrite(buf, f, photometric="minisblack", description=desc)
            data = buf.getvalue()
            info = tarfile.TarInfo(f"1/frame_{i:05d}.tiff")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_varying_but_equal_length_shells_roundtrip(tmp_path):
    """Per-frame metadata of a constant size is stored per frame and must still be exact."""
    frames = make_frames(n=8)
    src = tmp_path / "in.tar"
    # same length, different content -> shells differ but the fixed-stride blob is still valid
    _tar_with_varying_headers(src, frames, [f"frame{i:04d}" for i in range(len(frames))])

    wfz, out = tmp_path / "a.wfz", tmp_path / "out.tar"
    meta = compress(src, wfz)
    assert not meta["shells_uniform"]
    assert meta["n_distinct_shells"] == len(frames)
    decompress(wfz, out)
    assert sha256_file(src) == sha256_file(out)


def test_unequal_length_shells_are_refused_not_corrupted(tmp_path):
    """Different header sizes would break the reader's fixed-stride slicing, so refuse."""
    frames = make_frames(n=8)
    src = tmp_path / "in.tar"
    _tar_with_varying_headers(src, frames, ["x" * (10 + 7 * i) for i in range(len(frames))])
    with pytest.raises(NotImplementedError, match="header sizes"):
        compress(src, tmp_path / "a.wfz")


def test_uniform_shells_are_stored_once(tmp_path):
    frames = make_frames(n=64)
    src = tmp_path / "in.tar"
    write_tiff_tar(src, frames)
    meta = compress(src, tmp_path / "a.wfz")
    assert meta["shells_uniform"]
    assert meta["n_distinct_shells"] == 1


def test_corrupted_codestream_is_caught(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames())
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)

    blob = bytearray(wfz.read_bytes())
    blob[200] ^= 0xFF  # flip a bit inside the first codestream
    wfz.write_bytes(bytes(blob))
    with pytest.raises(Exception):
        decompress(wfz, tmp_path / "out.tar")
