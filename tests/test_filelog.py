"""The file-edit log has to be complete and honest, because it is what a human audits."""

from __future__ import annotations

import csv
import io
import tarfile

import numpy as np
import pytest
import tifffile

from wfcompress import compress, decompress, filelog
from wfcompress.frames import GeometryUnknown


def make_frames(n=8, rows=32, cols=32):
    rng = np.random.default_rng(0)
    y, x = np.mgrid[0:rows, 0:cols]
    base = (300 + 100 * np.sin(x / 5) * np.cos(y / 4)).astype(np.int32)
    return np.stack(
        [
            np.clip(base + i + rng.integers(0, 8, (rows, cols)), 0, 4095).astype(np.uint16)
            for i in range(n)
        ]
    )


def write_tiff_tar(path, frames):
    with tarfile.open(path, "w") as tf:
        for i, f in enumerate(frames):
            buf = io.BytesIO()
            tifffile.imwrite(buf, f, photometric="minisblack")
            data = buf.getvalue()
            info = tarfile.TarInfo(f"1/f{i}.tiff")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def rows_of(log):
    with open(log, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_log_records_creation_with_path_size_time_and_type(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames())
    log = filelog.ensure(tmp_path / "fileEditLog.csv")
    dst = tmp_path / "a.wfz"

    compress(src, dst, file_log=str(log))

    rows = rows_of(log)
    created = [r for r in rows if r["event"] == "create" and r["path"] == str(dst)]
    assert len(created) == 1
    assert int(created[0]["size_bytes"]) == dst.stat().st_size
    assert created[0]["timestamp_utc"].startswith("20")
    assert created[0]["pid"] and created[0]["host"]


def test_rewriting_an_existing_file_is_logged_as_modify_not_create(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames())
    log = filelog.ensure(tmp_path / "fileEditLog.csv")
    dst = tmp_path / "a.wfz"

    compress(src, dst, file_log=str(log))
    compress(src, dst, file_log=str(log))

    events = [r["event"] for r in rows_of(log) if r["path"] == str(dst)]
    assert events == ["create", "modify"]


def test_temporary_files_are_logged_created_and_removed(tmp_path):
    """Atomic writes leave nothing behind, but the audit should still show they happened."""
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames())
    log = filelog.ensure(tmp_path / "fileEditLog.csv")
    compress(src, tmp_path / "a.wfz", file_log=str(log))

    temps = [r for r in rows_of(log) if ".partial-" in r["path"]]
    assert [r["event"] for r in temps] == ["create", "delete"]
    assert not list(tmp_path.glob("*.partial-*"))


def test_a_failed_run_logs_the_discarded_temporary(tmp_path):
    src = tmp_path / "in.tar"
    with tarfile.open(src, "w") as tf:
        for i, f in enumerate(make_frames(n=3)):
            data = f.astype("<u2").tobytes()
            info = tarfile.TarInfo(f"1/frame-{i}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    log = filelog.ensure(tmp_path / "fileEditLog.csv")

    with pytest.raises(GeometryUnknown):
        compress(src, tmp_path / "a.wfz", file_log=str(log))

    rows = rows_of(log)
    assert not [r for r in rows if r["path"].endswith("a.wfz")], "no output was produced"
    assert not list(tmp_path.glob("*.partial-*"))


def test_decompress_output_is_logged(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames())
    wfz = tmp_path / "a.wfz"
    compress(src, wfz)
    log = filelog.ensure(tmp_path / "fileEditLog.csv")
    out = tmp_path / "out.tar"
    decompress(wfz, out, file_log=str(log))
    assert [r["event"] for r in rows_of(log) if r["path"] == str(out)] == ["create"]


def test_sidecars_are_logged(tmp_path):
    from wfcompress import sidecar

    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames())
    wfz = tmp_path / "a.wfz"
    meta = compress(src, wfz)
    log = filelog.ensure(tmp_path / "fileEditLog.csv")

    sidecar.write_readme(wfz, meta, file_log=str(log))
    sidecar.write_receipt(wfz, meta, file_log=str(log))
    sidecar.write_preview_frame(wfz, file_log=str(log))

    logged = {r["path"].rsplit(".", 1)[-1] for r in rows_of(log)}
    assert {"md", "json", "tif"} <= logged


def test_logging_never_breaks_the_run(tmp_path):
    """An audit failure must not be the thing that fails a compression."""
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames())
    unwritable = tmp_path / "nope" / "deep" / "log.csv"  # parent does not exist
    compress(src, tmp_path / "a.wfz", file_log=str(unwritable))
    assert (tmp_path / "a.wfz").exists()


def test_summarise_counts_events(tmp_path):
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames())
    log = filelog.ensure(tmp_path / "fileEditLog.csv")
    compress(src, tmp_path / "a.wfz", file_log=str(log))
    s = filelog.summarise(log)
    assert s["create"]["n"] >= 1
    assert s["create"]["bytes"] > 0


def test_summary_does_not_count_temp_renames_as_deletions(tmp_path):
    """A temporary being renamed into place is a `delete` of its path. Summing those alongside
    real deletions would report tens of GB removed on a run that removed nothing."""
    src = tmp_path / "in.tar"
    write_tiff_tar(src, make_frames())
    log = filelog.ensure(tmp_path / "fileEditLog.csv")
    compress(src, tmp_path / "a.wfz", file_log=str(log))

    s = filelog.summarise(log)
    assert s.get("delete", {"bytes": 0})["bytes"] == 0, "nothing persistent was deleted"
    assert s["transient"]["n"] == 2, "the temp create/delete pair is still recorded"
    assert filelog.is_transient("x/widefield.wfz.partial-123")
    assert not filelog.is_transient("x/widefield.wfz")
