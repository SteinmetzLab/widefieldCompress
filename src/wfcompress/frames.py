"""Splitting a tar member into pixels and the bytes that surround them.

A member is either a single-page uncompressed TIFF or a headerless raw frame. Either way we model
it as ``shell`` (everything that is not pixel data) plus a pixel array, so that

    join(split(raw)) == raw

exactly. Keeping the shell is what lets the original archive be rebuilt byte-for-byte; it is
typically a few KB of TIFF header and strip tables, identical across every frame in a session.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
import tifffile


class GeometryUnknown(ValueError):
    """Raised for headerless members when no frame shape was supplied.

    Deliberately fatal: guessing a shape from the member size silently produces a garbled but
    plausible-looking image, which is far worse than refusing to run.
    """


@dataclass(frozen=True)
class FrameLayout:
    """Where the pixels live inside a member, and how to interpret them."""

    shape: tuple[int, int]
    dtype: np.dtype
    px_start: int
    px_len: int
    is_tiff: bool

    @property
    def n_pixels(self) -> int:
        return int(np.prod(self.shape))


def detect_layout(sample: bytes, shape: tuple[int, int] | None = None) -> FrameLayout:
    """Work out the layout from one member's bytes.

    ``shape`` is only consulted for headerless members; TIFFs carry their own geometry.
    """
    if sample[:2] in (b"II", b"MM"):
        return _tiff_layout(sample)
    if shape is None:
        raise GeometryUnknown(
            "headerless frame member and no frame shape supplied - refusing to guess. "
            "Pass shape=(rows, cols); frames are not always square."
        )
    rows, cols = int(shape[0]), int(shape[1])
    px_len = rows * cols * 2
    if px_len != len(sample):
        raise GeometryUnknown(
            f"supplied shape {(rows, cols)} implies {px_len} bytes but the member is "
            f"{len(sample)} bytes"
        )
    return FrameLayout((rows, cols), np.dtype("<u2"), 0, px_len, is_tiff=False)


def _tiff_layout(sample: bytes) -> FrameLayout:
    with tifffile.TiffFile(io.BytesIO(sample)) as tf:
        if len(tf.pages) != 1:
            raise NotImplementedError(f"expected a single-page TIFF, got {len(tf.pages)} pages")
        page = tf.pages[0]
        if page.compression != 1:
            raise NotImplementedError(f"TIFF is already compressed ({page.compression!r})")
        shape = tuple(int(x) for x in page.shape)
        dtype = np.dtype(page.dtype)
        offsets = list(page.dataoffsets)
        counts = list(page.databytecounts)

    if len(shape) != 2:
        raise NotImplementedError(f"expected a 2-D image, got shape {shape}")
    if dtype.itemsize != 2:
        raise NotImplementedError(f"expected 16-bit samples, got {dtype}")
    if any(offsets[i] + counts[i] != offsets[i + 1] for i in range(len(offsets) - 1)):
        raise NotImplementedError("TIFF strips are not contiguous")

    byteorder = ">" if sample[:2] == b"MM" else "<"
    return FrameLayout(
        shape=shape,  # type: ignore[arg-type]
        dtype=np.dtype(byteorder + "u2"),
        px_start=int(offsets[0]),
        px_len=int(sum(counts)),
        is_tiff=True,
    )


def split(raw: bytes, layout: FrameLayout) -> tuple[np.ndarray, bytes]:
    """``raw`` -> (pixels, shell). The pixel array is a read-only view onto ``raw``."""
    pixels = np.frombuffer(
        raw, dtype=layout.dtype, count=layout.n_pixels, offset=layout.px_start
    ).reshape(layout.shape)
    shell = raw[: layout.px_start] + raw[layout.px_start + layout.px_len :]
    return pixels, shell


def join(pixels: np.ndarray, shell: bytes, layout: FrameLayout) -> bytes:
    """(pixels, shell) -> the original member bytes."""
    body = np.ascontiguousarray(pixels, dtype=layout.dtype).tobytes()
    if len(body) != layout.px_len:
        raise ValueError(f"pixel block is {len(body)} bytes, expected {layout.px_len}")
    return shell[: layout.px_start] + body + shell[layout.px_start :]
