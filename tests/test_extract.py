"""One-step extraction: .wfz -> a folder of the original frame files, or one flat .bin."""

from __future__ import annotations

import io
import json
import tarfile

import numpy as np
import pytest
import tifffile
from test_roundtrip import make_frames

from wfcompress import compress, extract
from wfcompress.extraction import UnsafeMemberName


def write_lexicographic_tar(path, frames, ext=".tiff", byteorder="<"):
    """A tar written the way the real archives are: members sorted by *name*, not by frame number.

    That is what makes storage order differ from acquisition order -- ``frame-10`` lands in slot 2,
    right after ``frame-1``. Any test of ordering that writes members in numeric order is testing
    nothing.
    """
    names = [f"1/frame-{i}{ext}" for i in range(len(frames))]
    order = sorted(range(len(frames)), key=lambda i: names[i])
    with tarfile.open(path, "w") as tf:
        for i in order:
            if ext:
                buf = io.BytesIO()
                tifffile.imwrite(buf, frames[i].astype(byteorder + "u2"),
                                 photometric="minisblack")
                data = buf.getvalue()
            else:
                data = frames[i].astype("<u2").tobytes()
            info = tarfile.TarInfo(names[i])
            info.size = len(data)
            info.mtime = 1700000000 + i
            tf.addfile(info, io.BytesIO(data))
    return names, order


def test_files_output_matches_tar_extraction_byte_for_byte(tmp_path):
    """The point of `extract` is to replace `decompress | tar -xf`. Prove it produces the same
    bytes, not merely the same pixels."""
    frames = make_frames(n=15, shift=4)
    src = tmp_path / "in.tar"
    write_lexicographic_tar(src, frames)
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)

    ours = tmp_path / "ours"
    r = extract(wfz, ours, fmt="files")
    assert r["pixels_verified"] is True
    assert r["n_frames"] == len(frames)

    theirs = tmp_path / "theirs"
    with tarfile.open(src) as tf:
        tf.extractall(theirs, filter="data")

    ours_files = sorted(p.relative_to(ours) for p in ours.rglob("*") if p.is_file())
    theirs_files = sorted(p.relative_to(theirs) for p in theirs.rglob("*") if p.is_file())
    assert ours_files == theirs_files
    for rel in ours_files:
        assert (ours / rel).read_bytes() == (theirs / rel).read_bytes(), rel


def test_files_output_keeps_the_original_modification_times(tmp_path):
    frames = make_frames(n=6)
    src = tmp_path / "in.tar"
    names, order = write_lexicographic_tar(src, frames)
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)

    out = tmp_path / "out"
    extract(wfz, out, fmt="files")
    with tarfile.open(src) as tf:
        for m in tf.getmembers():
            assert int((out / m.name).stat().st_mtime) == m.mtime


def test_bin_is_exactly_the_expected_size_and_in_acquisition_order(tmp_path):
    """rows * cols * n_frames * 2 bytes, frame k of the recording at offset k * frame_bytes."""
    rows, cols, n = 32, 50, 15
    frames = make_frames(n=n, rows=rows, cols=cols, shift=4)
    src = tmp_path / "in.tar"
    write_lexicographic_tar(src, frames)
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)

    out = tmp_path / "wf.bin"
    r = extract(wfz, out, fmt="bin")
    assert out.stat().st_size == rows * cols * n * 2
    assert r["bytes_written"] == out.stat().st_size
    assert r["order"] == "acquisition"
    assert r["pixels_verified"] is True

    got = np.memmap(out, dtype="<u2", mode="r").reshape(n, rows, cols)
    np.testing.assert_array_equal(got, frames)


def test_bin_storage_order_differs_from_acquisition_order(tmp_path):
    """If these two agreed, the acquisition-order test above would prove nothing."""
    rows, cols, n = 32, 50, 15
    frames = make_frames(n=n, rows=rows, cols=cols)
    src = tmp_path / "in.tar"
    _names, order = write_lexicographic_tar(src, frames)
    assert order != list(range(n)), "the fixture failed to scramble the order"

    wfz = tmp_path / "a.wfz"
    compress(src, wfz)
    acq, sto = tmp_path / "acq.bin", tmp_path / "sto.bin"
    extract(wfz, acq, fmt="bin", order="acquisition")
    extract(wfz, sto, fmt="bin", order="storage")

    assert acq.read_bytes() != sto.read_bytes()
    got = np.memmap(sto, dtype="<u2", mode="r").reshape(n, rows, cols)
    np.testing.assert_array_equal(got, frames[order])


def test_bin_from_headerless_archive(tmp_path):
    rows, cols, n = 32, 50, 12
    frames = make_frames(n=n, rows=rows, cols=cols)
    src = tmp_path / "in.tar"
    write_lexicographic_tar(src, frames, ext="")
    wfz = tmp_path / "a.wfz"
    compress(src, wfz, shape=(rows, cols))

    out = tmp_path / "wf.bin"
    extract(wfz, out, fmt="bin")
    got = np.memmap(out, dtype="<u2", mode="r").reshape(n, rows, cols)
    np.testing.assert_array_equal(got, frames)


def test_bin_sidecar_describes_the_file(tmp_path):
    """The .bin is headerless by design, so the geometry has to be recoverable from somewhere."""
    rows, cols, n = 32, 50, 10
    frames = make_frames(n=n, rows=rows, cols=cols)
    src = tmp_path / "in.tar"
    write_lexicographic_tar(src, frames)
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)

    out = tmp_path / "wf.bin"
    r = extract(wfz, out, fmt="bin")
    info = json.loads((tmp_path / "wf.bin.json").read_text())
    assert (info["rows"], info["cols"], info["n_frames"]) == (rows, cols, n)
    assert info["total_bytes"] == out.stat().st_size
    assert info["frame_order"] == "acquisition"
    assert r["sidecar"].endswith("wf.bin.json")


def test_big_endian_tiffs_produce_a_little_endian_bin_by_default(tmp_path):
    """The real archives are big-endian TIFFs (MM), while the headerless ones are little-endian
    raw. Transcribing the source bytes would mean two sessions that look identical needing
    different readers, so the .bin is normalised - same values, ordinary byte order.
    """
    rows, cols, n = 32, 50, 10
    frames = make_frames(n=n, rows=rows, cols=cols)
    src = tmp_path / "in.tar"
    write_lexicographic_tar(src, frames, byteorder=">")
    wfz = tmp_path / "a.wfz"
    meta = compress(src, wfz)
    assert meta["dtype"] == ">u2", "fixture did not produce a big-endian TIFF"

    out = tmp_path / "wf.bin"
    r = extract(wfz, out, fmt="bin")
    assert r["dtype"] == "<u2"
    assert r["source_dtype"] == ">u2"
    assert r["byteswapped"] is True
    assert r["pixels_verified"] is True, "the recorded hash must still be checkable after a swap"
    np.testing.assert_array_equal(
        np.memmap(out, dtype="<u2", mode="r").reshape(n, rows, cols), frames
    )
    assert json.loads((tmp_path / "wf.bin.json").read_text())["dtype"] == "<u2"

    keep = tmp_path / "src.bin"
    rk = extract(wfz, keep, fmt="bin", byteorder="source")
    assert rk["dtype"] == ">u2" and rk["byteswapped"] is False
    assert keep.read_bytes() != out.read_bytes()
    np.testing.assert_array_equal(
        np.memmap(keep, dtype=">u2", mode="r").reshape(n, rows, cols), frames
    )


def test_files_output_is_never_byteswapped(tmp_path):
    """Per-frame files must stay byte-identical to the archive, whatever --byteorder says."""
    frames = make_frames(n=6, rows=32, cols=50)
    src = tmp_path / "in.tar"
    write_lexicographic_tar(src, frames, byteorder=">")
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)

    out = tmp_path / "out"
    r = extract(wfz, out, fmt="files", byteorder="little")
    assert r["byteswapped"] is False
    theirs = tmp_path / "theirs"
    with tarfile.open(src) as tf:
        tf.extractall(theirs, filter="data")
    for p in sorted(out.rglob("*")):
        if p.is_file():
            assert p.read_bytes() == (theirs / p.relative_to(out)).read_bytes()


def test_frame_range_extracts_only_that_slice(tmp_path):
    rows, cols, n = 32, 50, 20
    frames = make_frames(n=n, rows=rows, cols=cols)
    src = tmp_path / "in.tar"
    write_lexicographic_tar(src, frames)
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)

    out = tmp_path / "slice.bin"
    r = extract(wfz, out, fmt="bin", first=5, last=9)
    assert r["n_frames"] == 4
    assert r["pixels_verified"] is False, "a partial extraction cannot check the whole-file hash"
    got = np.memmap(out, dtype="<u2", mode="r").reshape(4, rows, cols)
    np.testing.assert_array_equal(got, frames[5:9])


def test_existing_output_is_not_clobbered_without_overwrite(tmp_path):
    frames = make_frames(n=5)
    src = tmp_path / "in.tar"
    write_lexicographic_tar(src, frames)
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)

    out = tmp_path / "wf.bin"
    out.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        extract(wfz, out, fmt="bin")
    assert out.read_bytes() == b"precious"

    extract(wfz, out, fmt="bin", overwrite=True)
    assert out.stat().st_size == 40 * 48 * 5 * 2


def test_corrupted_codestream_is_caught_during_extraction(tmp_path):
    from wfcompress.codec import LosslessCheckFailed

    src = tmp_path / "in.tar"
    write_lexicographic_tar(src, make_frames(n=8))
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)

    blob = bytearray(wfz.read_bytes())
    blob[200] ^= 0xFF
    wfz.write_bytes(bytes(blob))
    with pytest.raises(LosslessCheckFailed, match="CRC"):
        extract(wfz, tmp_path / "out", fmt="files")


def test_member_names_that_escape_the_output_directory_are_refused(tmp_path):
    """Member names come from the archive, not the caller, so a traversal has to be refused."""
    frames = make_frames(n=3, rows=32, cols=50)
    src = tmp_path / "in.tar"
    with tarfile.open(src, "w") as tf:
        for i in range(len(frames)):
            data = frames[i].astype("<u2").tobytes()
            info = tarfile.TarInfo(f"../escaped-{i}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

    wfz = tmp_path / "a.wfz"
    compress(src, wfz, shape=(32, 50))
    with pytest.raises(UnsafeMemberName):
        extract(wfz, tmp_path / "out", fmt="files")
    assert not list(tmp_path.glob("escaped-*")), "nothing may be written outside the target"


def test_cli_extract_both_ways(tmp_path):
    from wfcompress.cli import main

    rows, cols, n = 32, 50, 10
    frames = make_frames(n=n, rows=rows, cols=cols)
    src = tmp_path / "in.tar"
    write_lexicographic_tar(src, frames)
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)

    assert main(["--quiet", "extract", str(wfz), str(tmp_path / "tifs")]) == 0
    assert len(list((tmp_path / "tifs").rglob("*.tiff"))) == n

    binp = tmp_path / "wf.bin"
    assert main(["--quiet", "extract", str(wfz), str(binp), "--bin"]) == 0
    got = np.memmap(binp, dtype="<u2", mode="r").reshape(n, rows, cols)
    np.testing.assert_array_equal(got, frames)


def test_zero_size_entries_are_recreated(tmp_path):
    """Real archives carry directory entries, sometimes only at the very end -- AL_0039 2025-10-02
    finishes with an empty `1/p0_g0/` after 426,324 frames. Dropping them would make the
    extraction quietly incomplete relative to `tar -xf`."""
    frames = make_frames(n=5, rows=32, cols=50)
    src = tmp_path / "in.tar"
    with tarfile.open(src, "w") as tf:
        d = tarfile.TarInfo("1/")
        d.type, d.mode = tarfile.DIRTYPE, 0o755
        tf.addfile(d)
        for i in range(len(frames)):
            data = frames[i].astype("<u2").tobytes()
            info = tarfile.TarInfo(f"1/frame-{i}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        trailing = tarfile.TarInfo("1/p0_g0/")
        trailing.type, trailing.mode = tarfile.DIRTYPE, 0o755
        tf.addfile(trailing)

    wfz = tmp_path / "a.wfz"
    compress(src, wfz, shape=(32, 50))
    out = tmp_path / "out"
    extract(wfz, out, fmt="files")
    assert (out / "1").is_dir()
    assert (out / "1" / "p0_g0").is_dir(), "trailing directory entry was dropped"
    assert len(list((out / "1").glob("frame-*"))) == 5


def test_extract_never_needs_the_intermediate_tar(tmp_path):
    """Regression guard on the whole premise: nothing tar-shaped may appear on disk."""
    frames = make_frames(n=8)
    src = tmp_path / "in.tar"
    write_lexicographic_tar(src, frames)
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)

    work = tmp_path / "work"
    work.mkdir()
    extract(wfz, work / "out", fmt="files")
    extract(wfz, work / "wf.bin", fmt="bin")
    assert not list(work.rglob("*.tar"))
