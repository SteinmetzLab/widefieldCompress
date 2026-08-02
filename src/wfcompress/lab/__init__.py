"""Steinmetz-lab specifics: where the data lives and how to run over all of it.

Everything here is site-specific and of no use to anyone else. It imports from
:mod:`wfcompress` and never the other way round, so the core stays reusable by anyone with a tar
of camera frames. ``tests/test_core_is_standalone.py`` enforces that direction.
"""

from .census import Census, TarRecord, scan
from .session import session_frame_shape

__all__ = ["Census", "TarRecord", "scan", "session_frame_shape"]
