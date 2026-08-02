# wfCompressTest — Phase 0.5 pilot

Source session: `Y:\Subjects\FD_010\2026-02-23\3\widefield.tar` (2026-02-23, Basler TIFF flavour).
**The source folder was not touched.** It was copied here and everything below happened in this
directory only.

## The three files

| file | size | what it is |
|---|---|---|
| `widefield.ORIGINAL.tar` | 3.01 GB | byte-for-byte copy of the session's `widefield.tar` |
| `widefield.wfz` | 1.04 GB | the compressed form — **2.88×, 65.3 % saved** |
| `widefield.RESTORED.tar` | 3.01 GB | `widefield.wfz` decompressed back to a tar |

## Result

```
76ffb41de5b7cec57375c5f68b069cb76ba85cc1498cc34abe4cef91af41c941  widefield.ORIGINAL.tar
76ffb41de5b7cec57375c5f68b069cb76ba85cc1498cc34abe4cef91af41c941  widefield.RESTORED.tar
```

**Byte-identical** — not merely same-pixels. The restored file is indistinguishable from the
original: same 4,753 tar entries, same member names, same order, same permissions and timestamps,
same TIFF headers, same trailing padding. `tar tvf` listings match exactly, and a frame extracted
from each with GNU `tar` (not with my code) compares equal with `cmp`.

## What the compressor does

4,752 frames of 560×560 `uint16`, big-endian, one TIFF per frame.

1. **Split each member** into its pixel block and its "shell" (the 4,626 B of TIFF header and strip
   tables that surround it). Every shell in this session is identical, so one copy is stored.
2. **Strip always-zero low bits.** This camera writes 10-bit data left-shifted into bits 4–13, so
   the bottom 4 bits of every sample are hard zero. Detected automatically, recorded, and undone on
   read. Without this step the same file compresses only ~1.6× instead of 2.88×.
3. **JPEG-LS, lossless mode** (`near=0`) on the shifted pixels — no quantisation anywhere.
4. **Verify while writing:** every frame is decoded again immediately after encoding and compared
   to the source. A single differing pixel aborts the run and leaves the input untouched.

Rebuild information (all 4,753 tar headers, the shell, the trailing bytes) is kept in a small zip
appended to the end of the `.wfz`. It costs **0.07 MB — 0.01 % of the output**, which is why
byte-identical restore is worth doing rather than settling for same-pixels.

## Speed (over SMB from this Windows workstation, 8 threads)

- compress **0.7 min**, 68 MB/s — includes the per-frame verify decode
- decompress **0.5 min**, 91 MB/s

Both will be substantially faster run on the server itself, with no network in the path.

## Inspecting it yourself

```bash
tar tvf widefield.RESTORED.tar | head
tar xf widefield.RESTORED.tar -C /tmp 3/Basler_acA2440-75um__23040354__20260223_121706009_2000.tiff
python wfcompress.py verify widefield.ORIGINAL.tar widefield.RESTORED.tar
```

The `.wfz` footer is a plain zip; `meta.json` inside it records geometry, bit shift, frame count and
the pixel SHA-256:

```bash
python -c "import zipfile,struct;f=open('widefield.wfz','rb');f.seek(8);o=struct.unpack('<Q',f.read(8))[0];f.seek(o);print(zipfile.ZipFile(__import__('io').BytesIO(f.read())).read('meta.json').decode())"
```

## Caveats before this scales

- One session, one flavour. The `frame-N` flavour (headerless raw, no geometry in the file, and not
  always square) is not exercised here and needs its own pilot.
- Nothing has been deleted and nothing was written outside this directory.
