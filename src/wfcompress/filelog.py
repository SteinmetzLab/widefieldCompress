"""An append-only record of every file this tool creates, replaces or removes.

The point is auditability during the window in which mistakes are still recoverable: the lab share
keeps deleted versions for 60 days, so a human reviewing this log has that long to catch anything
that went wrong. It is deliberately dumb and complete rather than clever - every mutation gets a
row, including the temporary files that atomic writes create and remove.

Rows are appended one line at a time in ``"a"`` mode, so concurrent worker processes interleave
rows without corrupting each other; a single short line written to an O_APPEND handle does not
tear. Order across processes is therefore arrival order, not a global sequence.
"""

from __future__ import annotations

import csv
import io
import os
import platform
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ["timestamp_utc", "event", "path", "size_bytes", "host", "pid", "note"]

#: create   - a file that did not exist now does
#: modify   - a file that already existed was replaced
#: delete   - a file that existed no longer does
EVENTS = ("create", "modify", "delete")


def ensure(log_path: str | Path) -> Path:
    """Create the log with its header if absent. Call once, from the parent process."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists() or log_path.stat().st_size == 0:
        with log_path.open("w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=FIELDS).writeheader()
    return log_path


def record(
    log_path: str | Path | None,
    event: str,
    path: str | Path,
    size_bytes: int | None = None,
    note: str = "",
) -> None:
    """Append one row. Never raises - an audit failure must not abort real work.

    ``size_bytes`` is taken from the file when not given, which is why ``delete`` events must be
    recorded *before* the file goes away.
    """
    if log_path is None:
        return
    try:
        path = Path(path)
        if size_bytes is None:
            size_bytes = path.stat().st_size if path.exists() else ""
        row = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "path": str(path),
            "size_bytes": size_bytes,
            "host": platform.node(),
            "pid": os.getpid(),
            "note": note,
        }
        buf = io.StringIO()
        csv.DictWriter(buf, fieldnames=FIELDS).writerow(row)
        line = buf.getvalue()
        with open(log_path, "a", newline="", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001 - logging must never be the thing that fails a run
        pass


def record_write(log_path, path, existed_before: bool, note: str = "") -> None:
    """Record a completed write as create or modify, depending on what was there before."""
    record(log_path, "modify" if existed_before else "create", path, note=note)


TRANSIENT_MARKER = ".partial-"


def is_transient(path: str) -> bool:
    """Whether a row refers to one of the temporary files an atomic write creates and renames away.

    These are logged for completeness, but they must not be added up alongside real deletions: a
    temporary being renamed into place is recorded as a `delete` of its path, and summing those
    would report tens of GB "deleted" on a run that removed nothing at all.
    """
    return TRANSIENT_MARKER in path


def summarise(log_path: str | Path) -> dict:
    """Counts and total bytes per event, splitting persistent files from temporaries.

    Returns ``{event: {"n", "bytes"}}`` for persistent files plus a single ``"transient"`` entry
    covering the temporary-file rows, so a reader is not misled about what was actually removed.
    """
    out: dict[str, dict[str, int]] = {}
    with Path(log_path).open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = "transient" if is_transient(row["path"]) else row["event"]
            e = out.setdefault(key, {"n": 0, "bytes": 0})
            e["n"] += 1
            if key == "transient":
                continue
            try:
                e["bytes"] += int(row["size_bytes"] or 0)
            except ValueError:
                pass
    return out
