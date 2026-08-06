"""Lossless compression of tar archives of scientific camera frames.

The archives this targets hold one uncompressed frame per member - either a single-page TIFF or a
headerless raw block - which is a common way to get data off an acquisition machine quickly. That
format is convenient to write and extremely wasteful to keep.

    from wfcompress import compress, decompress, extract, WfzReader

    compress("widefield.tar", "widefield.wfz")
    decompress("widefield.wfz", "restored.tar")     # byte-identical to the input

    extract("widefield.wfz", "frames/")             # straight to the original TIFFs
    extract("widefield.wfz", "wf.bin", fmt="bin")   # or one flat binary, acquisition order

    with WfzReader("widefield.wfz") as r:
        frame = r.frame(0)

Nothing in this package knows about any particular server or directory layout. Site-specific
inventory and batch tooling lives in :mod:`wfcompress.lab`, which imports from here and never the
other way round.
"""

from . import filelog
from .codec import LosslessCheckFailed, compress, decompress, sha256_file, verify
from .container import read_meta
from .extraction import extract
from .frames import GeometryUnknown
from .provenance import provenance
from .reader import WfzReader

__all__ = [
    "compress",
    "decompress",
    "extract",
    "sha256_file",
    "verify",
    "filelog",
    "read_meta",
    "provenance",
    "WfzReader",
    "LosslessCheckFailed",
    "GeometryUnknown",
]
