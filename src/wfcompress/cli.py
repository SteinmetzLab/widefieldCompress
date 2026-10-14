"""Command line interface: ``wfcompress <command> ...``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import codec, container, extraction, sidecar, wfacq
from .reader import WfzReader


def _progress(label: str, px_per_frame: int):
    def report(done: int, total: int, elapsed: float):
        rate = done * px_per_frame / 1e6 / max(elapsed, 1e-9)
        end = "\n" if done == total else "\r"
        print(f"  {label} {done:>8,d}/{total:,d} frames  {rate:6.1f} MB/s", end=end, flush=True)

    return report


def _resolve_shape(args) -> tuple[int, int] | None:
    """--shape wins; otherwise take the geometry from a reference array if one was given."""
    if args.shape:
        return (int(args.shape[0]), int(args.shape[1]))
    if args.shape_from:
        import numpy as np

        ref = np.load(args.shape_from, mmap_mode="r")
        if ref.ndim != 2:
            raise SystemExit(f"{args.shape_from} is {ref.ndim}-D; need a 2-D reference image")
        return (int(ref.shape[0]), int(ref.shape[1]))
    return None


def cmd_compress_session(args) -> int:
    """A wfAcq session folder: compress, prove the rebuild by streaming it, then the sidecars."""
    if args.shape or args.shape_from:
        raise SystemExit("--shape is not used for a wfAcq session; geometry comes from "
                         "wf_meta.json")
    if args.no_sidecar:
        raise SystemExit("--no-sidecar is not available for a wfAcq session: the receipt is the "
                         "record that the rebuild was verified")
    r = wfacq.convert_session(
        args.src,
        args.dst,
        threads=args.threads,
        progress=None if args.quiet else _progress("compress", 1),
        verify_progress=None if args.quiet else _progress("verify", 1),
        min_age_s=codec.DEFAULT_MIN_AGE_S if args.min_age_s is None else args.min_age_s,
    )
    saved = 100 * (1 - r["output_bytes"] / r["source_bytes"])
    print(
        f"  {r['source_bytes']/1e9:.2f} GB -> {r['output_bytes']/1e9:.2f} GB   "
        f"x{r['ratio']:.2f}   {saved:.1f}% saved   compress {r['compress_s']/60:.1f} min, "
        f"verify {r['verify_s']/60:.1f} min"
    )
    print(f"  {r['wfz']}: rebuilds wf.bin BYTE-IDENTICAL, sha256 {r['bin_sha256']}")
    return 0


def cmd_compress(args) -> int:
    if Path(args.src).is_dir():
        return cmd_compress_session(args)
    if args.dst is None:
        raise SystemExit("compress: the output path is required for a tar")
    if args.min_age_s is not None:
        raise SystemExit("--min-age-s applies to wfAcq session folders only")
    shape = _resolve_shape(args)
    meta = codec.compress(
        args.src,
        args.dst,
        shape=shape,
        threads=args.threads,
        progress=None if args.quiet else _progress("compress", 1),
    )
    saved = 100 * (1 - meta["output_bytes"] / meta["source_bytes"])
    print(
        f"  {meta['source_bytes']/1e9:.2f} GB -> {meta['output_bytes']/1e9:.2f} GB   "
        f"x{meta['ratio']:.2f}   {saved:.1f}% saved   "
        f"{meta['elapsed_s']/60:.1f} min   shift={meta['shift']}"
    )
    if not args.no_sidecar:
        sidecar.write_readme(args.dst, meta)
        sidecar.write_receipt(args.dst, meta)
        preview = sidecar.write_preview_frame(args.dst)
        if preview:
            print(f"  preview frame -> {preview.name}")
    return 0


def cmd_decompress(args) -> int:
    result = codec.decompress(
        args.src,
        args.dst,
        threads=args.threads,
        progress=None if args.quiet else _progress("decompress", 1),
    )
    print(
        f"  wrote {result['output_bytes']/1e9:.2f} GB   pixel hash OK   "
        f"size {'matches' if result['size_matches'] else 'DIFFERS FROM'} the original"
    )
    return 0 if result["size_matches"] else 1


def cmd_extract(args) -> int:
    fmt = "bin" if args.bin else "files"
    r = extraction.extract(
        args.src,
        args.dst,
        fmt=fmt,
        first=args.frames[0] if args.frames else None,
        last=args.frames[1] if args.frames else None,
        order=args.order,
        byteorder=args.byteorder,
        threads=args.threads,
        overwrite=args.overwrite,
        progress=None if args.quiet else _progress("extract", 1),
    )
    rows, cols = r["shape"]
    if fmt == "bin":
        print(
            f"  {r['n_frames']:,} frames x {rows}x{cols} {r['dtype']} "
            f"= {r['bytes_written']:,} bytes -> {args.dst}"
        )
        print(f"  read it with: np.memmap(path, dtype='{r['dtype']}', mode='r')"
              f".reshape(-1, {rows}, {cols})")
        if r["byteswapped"]:
            print(f"  source pixels are {r['source_dtype']} (big-endian TIFF); written "
                  f"little-endian so ordinary readers work. --byteorder source keeps the "
                  f"archive's own bytes.")
        print(f"  geometry also written to {Path(r['sidecar']).name}")
    else:
        print(f"  {r['n_frames']:,} files ({r['bytes_written']/1e9:.2f} GB) -> {args.dst}")
    if r["order"] == "storage" and not r["temporal_order_known"]:
        print("  NOTE: this archive's member names carry no frame number, so frames are in "
              "archive order, which may not be acquisition order")
    elif fmt == "bin":
        print(f"  frame order: {r['order']}")
    print("  pixel SHA-256 matches the value recorded at compression time"
          if r["pixels_verified"]
          else "  partial extraction: per-frame CRC-32 checked, whole-archive hash not applicable")
    return 0


def cmd_check(args) -> int:
    """Prove a .wfz rebuilds its source archive, writing nothing."""
    r = codec.verify(args.src, threads=args.threads,
                     progress=None if args.quiet else _progress("verify", 1))
    if "bin_sha256" in r:
        print(f"  rebuilt {r['rebuilt_bytes']/1e9:.2f} GB   wf.bin BYTE-IDENTICAL, "
              f"{r['n_session_files']} session files match")
        print(f"  wf.bin sha256 {r['bin_sha256']}")
        return 0
    if r["byte_identical"] is None:
        print("  pixel hash OK; this file predates source_tar_sha256 so byte-identity "
              "cannot be checked without the original")
        return 0
    print(f"  rebuilt {r['rebuilt_bytes']/1e9:.2f} GB   sha256 {r['tar_sha256'][:16]}...   "
          f"BYTE-IDENTICAL to the source archive")
    return 0


def cmd_restore(args) -> int:
    r = wfacq.restore_session(args.src, args.dst, threads=args.threads,
                              progress=None if args.quiet else _progress("restore", 1),
                              only_bin=args.bin_only)
    print(f"  wrote {', '.join(r['files'])} -> {args.dst}   every file matches its recorded "
          f"SHA-256")
    return 0


def cmd_verify(args) -> int:
    a, b = codec.sha256_file(args.a), codec.sha256_file(args.b)
    print(f"  {a}  {Path(args.a).name}")
    print(f"  {b}  {Path(args.b).name}")
    same = a == b
    print("  IDENTICAL" if same else "  *** DIFFERENT ***")
    return 0 if same else 1


def cmd_info(args) -> int:
    meta = container.read_meta(args.src)
    print(json.dumps(meta, indent=2, sort_keys=True))
    return 0


def cmd_peek(args) -> int:
    with WfzReader(args.src) as r:
        print(f"  {r.n_frames:,} frames of {r.shape} {r.dtype}")
        f = r.frame(args.frame)
        print(f"  frame {args.frame}: min={f.min()} max={f.max()} mean={f.mean():.1f}")
        print(f"  member name: {r.member_name(args.frame)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wfcompress", description=__doc__)
    p.add_argument("--threads", type=int, default=codec.DEFAULT_THREADS)
    p.add_argument("--quiet", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser(
        "compress",
        help="tar of frames, or a wfAcq session folder -> .wfz",
        description="Compress a tar of frames, or a cleanly closed wfAcq session folder. For a "
                    "session, the .wfz holds wf.bin and every small session file; it is verified "
                    "by streaming the rebuild before the receipt is written. Nothing is deleted.",
    )
    c.add_argument("src", help="a .tar, or a wfAcq session folder")
    c.add_argument("dst", nargs="?",
                   help="output .wfz. Required for a tar; for a session folder it defaults to "
                        "SRC/widefield.wfz, the name DataBrowser looks for")
    c.add_argument("--min-age-s", type=float, default=None,
                   help="wfAcq sessions only: refuse if any session file is newer than this "
                        f"(default {codec.DEFAULT_MIN_AGE_S} s); 0 turns the check off")
    c.add_argument(
        "--shape",
        type=int,
        nargs=2,
        metavar=("ROWS", "COLS"),
        help="frame geometry; required only for headerless raw archives",
    )
    c.add_argument(
        "--shape-from",
        metavar="REF.npy",
        help="take the frame geometry from a 2-D .npy (e.g. the session's meanImage.npy) "
             "instead of spelling it out with --shape",
    )
    c.add_argument("--no-sidecar", action="store_true",
                   help="tar only: skip the README/receipt files")
    c.set_defaults(func=cmd_compress)

    d = sub.add_parser("decompress", help=".wfz -> the original tar, byte-identical")
    d.add_argument("src")
    d.add_argument("dst")
    d.set_defaults(func=cmd_decompress)

    x = sub.add_parser(
        "extract",
        help=".wfz -> a folder of the original frame files, or one flat .bin",
        description="Decode straight to usable data, skipping the intermediate tar. Without "
                    "--bin you get the original per-frame files, byte-identical, with their "
                    "original names and modification times. With --bin you get a single "
                    "headerless binary of rows*cols*n_frames*2 bytes, frames in acquisition "
                    "order, like a SpikeGLX .ap.bin.",
    )
    x.add_argument("src")
    x.add_argument("dst", help="output directory, or the output file when --bin is given")
    x.add_argument("--bin", action="store_true",
                   help="write one flat headerless binary instead of a folder of frame files")
    x.add_argument("--frames", type=int, nargs=2, metavar=("FIRST", "LAST"),
                   help="extract only frames [FIRST, LAST); indices are in --order")
    x.add_argument("--order", choices=("acquisition", "storage"), default="acquisition",
                   help="acquisition (default) reorders frames to the order they were recorded; "
                        "storage keeps the order they sit in inside the archive, which for these "
                        "tars is lexicographic by name (frame-0, frame-1, frame-10, ...)")
    x.add_argument("--byteorder", choices=("little", "source"), default="little",
                   help="--bin only. Part of this corpus is big-endian TIFF and part is "
                        "little-endian raw; 'little' (default) writes little-endian either way so "
                        "the same reader works for every session, 'source' keeps the archive's "
                        "own bytes")
    x.add_argument("--overwrite", action="store_true", help="replace existing output files")
    x.set_defaults(func=cmd_extract)

    rs = sub.add_parser(
        "restore",
        help="session .wfz -> wf.bin and the session files, byte-identical",
        description="Write wf.bin and every stored session file into DIR, normally a new, "
                    "empty folder. Refuses if any of them already exists there; nothing is ever "
                    "replaced. --bin-only writes wf.bin alone, to put it back into a session "
                    "folder whose small files are still present.",
    )
    rs.add_argument("src")
    rs.add_argument("dst", metavar="DIR")
    rs.add_argument("--bin-only", action="store_true", help="write wf.bin only")
    rs.set_defaults(func=cmd_restore)

    v = sub.add_parser("verify", help="compare the sha256 of two files")
    v.add_argument("a")
    v.add_argument("b")
    v.set_defaults(func=cmd_verify)

    ck = sub.add_parser("check", help="prove a .wfz rebuilds its source, writing nothing")
    ck.add_argument("src")
    ck.set_defaults(func=cmd_check)

    i = sub.add_parser("info", help="print a .wfz's metadata")
    i.add_argument("src")
    i.set_defaults(func=cmd_info)

    k = sub.add_parser("peek", help="decode one frame and report its statistics")
    k.add_argument("src")
    k.add_argument("--frame", type=int, default=0)
    k.set_defaults(func=cmd_peek)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
