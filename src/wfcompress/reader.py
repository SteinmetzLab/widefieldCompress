"""Random access to frames in a .wfz, without rebuilding the tar.

Compression destroys the constant member stride the original archives had, so the container keeps
an explicit offset index; this class is the thing that uses it.
"""

from __future__ import annotations

import zlib
from pathlib import Path

import imagecodecs
import numpy as np

from . import container


class WfzReader:
    """Read frames from a .wfz.

    >>> with WfzReader("widefield.wfz") as r:      # doctest: +SKIP
    ...     r.n_frames, r.shape
    ...     frame = r.frame(0)
    """

    def __init__(self, path: str | Path, verify_crc: bool = True):
        self.path = Path(path)
        self.footer = container.read_footer(self.path)
        self.meta = self.footer.meta
        self.index = self.footer.index
        self.verify_crc = verify_crc
        self._dtype = np.dtype(self.meta["dtype"])
        self._shift = self.meta["shift"]
        self._fh = open(self.path, "rb")

    # -- context manager -------------------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if not self._fh.closed:
            self._fh.close()

    # -- properties ------------------------------------------------------------------------
    @property
    def n_frames(self) -> int:
        return int(self.meta["n_frames"])

    @property
    def shape(self) -> tuple[int, int]:
        return tuple(self.meta["shape"])  # type: ignore[return-value]

    @property
    def dtype(self) -> np.dtype:
        return self._dtype

    def __len__(self) -> int:
        return self.n_frames

    # -- reading ---------------------------------------------------------------------------
    def frame(self, i: int) -> np.ndarray:
        """Frame ``i`` as it was acquired, including the original bit shift."""
        if not 0 <= i < self.n_frames:
            raise IndexError(f"frame {i} out of range (0..{self.n_frames - 1})")
        offset, length, crc = (int(x) for x in self.index[i])
        self._fh.seek(offset)
        code = self._fh.read(length)
        if self.verify_crc and zlib.crc32(code) != np.uint32(crc):
            raise ValueError(f"frame {i}: stored codestream fails its CRC")
        g = imagecodecs.jpegls_decode(code)
        return (g.astype(np.uint32) << self._shift).astype(self._dtype).reshape(self.shape)

    def frames(self, indices) -> np.ndarray:
        """Stack of frames ``(n, rows, cols)``."""
        return np.stack([self.frame(int(i)) for i in indices])

    def __getitem__(self, key):
        if isinstance(key, slice):
            return self.frames(range(*key.indices(self.n_frames)))
        return self.frame(int(key))

    def member_name(self, i: int) -> str:
        """Original tar member name of frame ``i``."""
        names = self._member_names()
        return names[i]

    def _member_names(self) -> list[str]:
        if not hasattr(self, "_names"):
            blob, out = self.footer.tar_headers, []
            for k in range(0, len(blob), 512):
                h = blob[k : k + 512]
                size = int(h[124:136].rstrip(b"\0 ").decode("ascii", "replace") or "0", 8)
                if size:
                    out.append(h[:100].rstrip(b"\0").decode("utf-8", "surrogateescape"))
            self._names = out
        return self._names
