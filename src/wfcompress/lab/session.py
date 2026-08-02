"""Lab session-folder conventions.

Sessions look like ``<root>/Subjects/<subject>/<YYYY-MM-DD>/<exp>/`` and hold, beside the raw
``widefield.tar``, the SVD outputs produced by the cortex-lab widefield pipeline::

    blue/svdSpatialComponents.npy    blue/meanImage.npy
    violet/...                       corr/...
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


def session_frame_shape(tar_path: str | Path) -> tuple[int, int] | None:
    """Frame geometry for an archive of headerless raw frames, from the session's mean image.

    Archives of TIFFs carry their own geometry and do not need this. Returns None when the mean
    image is absent, which must be treated as fatal rather than guessed around - frames are not
    always square, so a wrong shape produces a garbled image that still looks plausible.
    """
    for channel in ("blue", "violet"):
        mean_image = Path(tar_path).parent / channel / "meanImage.npy"
        if mean_image.is_file():
            shape = np.load(mean_image, mmap_mode="r").shape
            if len(shape) == 2:
                return (int(shape[0]), int(shape[1]))
    return None


def has_svd(tar_path: str | Path) -> bool:
    """True when the SVD pipeline has already run, i.e. the tar is not the only copy."""
    return (Path(tar_path).parent / "blue" / "svdSpatialComponents.npy").is_file()


def session_id(tar_path: str | Path) -> str:
    """``subject/date/exp`` for logging and receipts."""
    p = Path(tar_path).parent
    return "/".join(p.parts[-3:])
