"""Record what produced a file, so a .wfz found in ten years explains itself."""

from __future__ import annotations

import platform
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

REPO_URL = "https://github.com/SteinmetzLab/widefieldCompress"


@lru_cache(maxsize=1)
def git_commit() -> str | None:
    """Commit hash of this checkout, or None when installed from a wheel."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            try:
                out = subprocess.run(
                    ["git", "-C", str(parent), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
                dirty = subprocess.run(
                    ["git", "-C", str(parent), "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                ).stdout.strip()
                return out.stdout.strip() + ("+dirty" if dirty else "")
            except (subprocess.SubprocessError, OSError):
                return None
    return None


@lru_cache(maxsize=1)
def version() -> str:
    try:
        from importlib.metadata import version as _v

        return _v("wfcompress")
    except Exception:  # noqa: BLE001 - provenance must never break a run
        return "unknown"


def provenance() -> dict:
    return {
        "tool": "wfcompress",
        "version": version(),
        "repo": REPO_URL,
        "git_commit": git_commit(),
        "written_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": platform.node(),
        "platform": platform.platform(),
        "python": platform.python_version(),
    }
