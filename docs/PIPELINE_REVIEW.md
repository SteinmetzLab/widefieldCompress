# Review of `Dropbox/code/Pipelines/widefield`

Read-only review, 2026-08-07. Nothing in that folder has been edited.

24 files: 23 MATLAB, one Python. The live path is
`svdScript.m` → `loadAndSVDf.m` → (`alignBlueVioletExposures` → SVD → tar to server → delete local
TIFFs → `computeWidefieldTimestamps` → `hemoCorrect`), with `tarFrames.m` as a standalone
transfer-only script and `reRunSVDf.m` as the reprocessing entry point.

Findings are ordered by what they can cost you, not by how hard they are to fix.

---

## A. Can destroy data or silently corrupt results

### A1. The tar archives the whole session folder, not the frames — this is why the ephys got in

`tarFrames.m:39` and `loadAndSVDf.m:181`:

```matlab
status = my7zTar(tarFile, {[rootDir '\']});
```

`rootDir` is the session directory. Everything in it goes into `widefield.tar` — including
subdirectories. That is exactly how **21 sessions (3.36 TB) ended up with a whole SpikeGLX
recording inside `widefield.tar`**: `p0_g0/`, `p0_g0_imec0/`, a 10–82 GB `.ap.bin`, its `.ap.meta`
and `p0.missed_samples.imec0.txt`, all sitting behind the frames. 1.21 TB of that is duplicated
ephys. The archives themselves are the evidence: member names are `1/p0_g0/…`, i.e. 7-Zip was
handed the directory and walked it.

The matching half of the explanation is `tarFrames.m:63`:

```matlab
delete(fullfile(rootDir, '*.tif*'));
```

Only the TIFFs are deleted, so the ephys stays behind locally and is later copied to the server by
its own route. That is why every one of the 21 also has the recording unpacked on the share at a
byte-identical size — nothing was lost, it was duplicated.

**Fix:** build the file list explicitly and pass it, rather than handing 7-Zip a directory. The
frames are already enumerated a few lines earlier as `allFn`.

### A2. The "did the transfer work?" check cannot detect a bad transfer

`tarFrames.m:51-58`, duplicated at `loadAndSVDf.m:194-201`:

```matlab
d = dir(rootDir);
localSize = sum([d.bytes]);
...
if serverSize <= localSize   % refuse to delete
```

Three problems, in increasing order of seriousness:

1. **`dir` is not recursive.** A subdirectory contributes its *entry*, not its contents. So
   whenever the session folder has subfolders — precisely the A1 cases — `localSize` is a large
   under-count and the check passes trivially.
2. **It compares only total size.** A tar of the right length containing garbage passes.
3. **The local TIFFs are deleted on the strength of it.**

**Fix:** verify, don't estimate. `7z t` tests the archive; `wfcompress check` proves byte-identity
against a hash taken during writing. At an absolute minimum, count recursively.

### A3. A truncated tar from an interrupted run is treated as a finished one

`tarFrames.m:38`, `loadAndSVDf.m:180`:

```matlab
if ~isfile(tarFile)
    status = my7zTar(...);
else
    status = 0;          % <- assumed good because the name exists
end
```

If a previous run died mid-write, the partial tar is on the server under its final name. This
takes `status = 0`, then applies the A2 size check — which the partial file may well pass, because
`localSize` is under-counted. That is a live path to deleting frames that were never fully
transferred.

**Fix:** write to a temporary name and rename only on success (this is what `wfcompress` does
internally, for the same reason), or verify any pre-existing file before trusting it.

### A4. Frame timestamps assume strict blue/violet alternation; the SVD does not

The one I would fix first on scientific grounds.

`loadAndSVDf.m:23-30` assigns each frame to a colour from the **measured LED traces**, and
explicitly drops frames that show both:

```matlab
[frameTimes, blueFrames, violetFrames, doubleFrames, blankFrames] = alignBlueVioletExposures(...);
blueFrames(doubleFrames) = 0;
violetFrames(doubleFrames) = 0;
```

`computeWidefieldTimestamps.m:16`, which writes the timestamps that sit next to those SVD
components, instead assumes **perfect alternation**:

```matlab
theseFrT = frTimes( mod(0:numel(frTimes)-1, numel(colors)) == (q-1) );
```

After the first double or blank frame these two disagree, and **every subsequent timestamp is
attached to the wrong frame**. Double frames are known to occur — `checkBVW.m` exists solely to
find them, `loadAndSVDf.m:25` warns about them, and `badBVWFolders.mat` is a saved list of sessions
that had them.

Two smaller inconsistencies live in the same place:

- `computeWidefieldTimestamps` uses Schmitt thresholds `[1 2]`; `alignBlueVioletExposures` uses
  `[1.1 2.2]`. Different thresholds can yield different flip counts.
- `alignBlueVioletExposures` shifts to mid-exposure (`flipsUp + frameLength/2`);
  `computeWidefieldTimestamps` does not. So `frameTimes.timestamps.npy` and
  `svdTemporalComponents.timestamps.npy` are systematically **half a frame apart** even when
  nothing else is wrong.

**Fix:** derive the per-colour timestamps from the same vectors the SVD was built from —
`frameTimes(blueFrames==1)` and `frameTimes(violetFrames==1)` — and delete the alternation logic
entirely.

### A5. Nothing checks that the TIFF count matches the exposure count

`loadAndSVDf.m:45,73,80`. `q` indexes both `allFn` (files on disk) and `blueFrames` (exposures
found in Timeline), with no assertion that they are the same length. One extra or missing file
shifts the colour assignment for the rest of the session, and the SVD then mixes blue and violet
frames.

`hemoCorrect.m:43-46` already acknowledges the symptom and papers over it:

```matlab
warning('Some blue frames are missing -- this should be dealt with in the SVD code...');
```

**Fix:** assert `numel(allFn) == numel(blueFrames)` before the loop, and stop if it fails.

### A6. Unassigned frames come back as NaN, and MATLAB treats NaN as true

`alignBlueVioletExposures.m:45-46` initialises `blueFrames = nan(size(frameTimes))`. The
assignment loop (53-71) breaks when it runs out of flips, so an exposure whose falling edge lies
past the end of the timeline trace — a recording stopped mid-frame — stays NaN.

In `loadAndSVDf.m:80`, `if blueFrames(q)` with `q` NaN is **true** in MATLAB, so that frame is
written to blue. And `sum(blueFrames)` becomes NaN, which flows straight into
`svdOps.Nframes = nFr(v)`.

**Fix:** initialise to `false(size(frameTimes))`, or check for NaN and error.

### A7. `alignBlueVioletExposures` errors on its own early-return path

Lines 31-35 return when fewer than two frames are found, but `blueFrames` and `violetFrames` were
never assigned at that point, so MATLAB raises "Output argument not assigned" instead.

### A8. A single-colour session writes its SVD into the wrong folder

`loadAndSVDf.m` builds `vids` conditionally (lines 116-122) — it holds `blue`, `violet`, or both,
depending on which LEDs were actually on. But the SVD loop at line 143 then indexes three *fixed*
two-element arrays with the position in `vids`:

```matlab
for v = 1:length(vids)
    svdOps.RegFile = fullfile(tempProcDir, sprintf('%s.dat', vids{v}));   % correct
    svdOps.Nframes = nFr(v);        % nFr is always [blue violet]
    svdOps.mimg    = mnImg{v};      % mnImg is always {blue violet}
    ...
    writeUVtoNPY(U, V, fullfile(root{v}, ...));   % root is always {blue violet}
```

For a violet-only session `vids = {'violet'}`, so `v = 1` reads `violet.dat` but uses the **blue**
mean image, `nFr(1) = sum(blueFrames) = 0`, and writes the result into the **blue** folder. Also,
lines 127-131 write `meanImage.npy` into both folders unconditionally, so a blue-only session gets
a violet `meanImage.npy` full of zeros.

**Fix:** index by colour name throughout, not by position in `vids`.

---

## B. Broken, dead, or does nothing

### B1. `loadAndSVD.m` does not parse

`caxis([-7000 7000]*/2)` on lines 22 and 24 is a syntax error. The file also targets
`\\steinmetzsuper1…` (retired) with a hardcoded 2019 session, and — importantly — writes
`blue.dat` / `violet.dat` **into the directory it then tars** (lines 35-36 vs 185), which would
sweep two full-size uncompressed movies into the archive.

It never ran against real data: I checked all 1,120 archives on the server and none contains a
`.dat` member. `loadAndSVDf.m` supersedes it and correctly writes those to `E:\tempProc`.
**Delete it or move it to an `archive/` folder** — as it stands it is a trap.

### B2. `reRunSVDf.m` untars to the wrong drive

`reRunSVDf.m:12` sets `tempDir = 'D:\data'`, but `loadAndSVDf.m:5` reads from `E:\data`. As
written it cannot work. Also: `tempFolder` omits the experiment number (survives only because the
tar stores a leading `<en>/`), "Data copied" is printed before the copy (line 25), and
`disp('here')` debug output remains at lines 39 and 44. Nothing removes the untarred session
afterwards, which is 100–300 GB of local disk.

### B3. `widefield_deconv.py` crashes whenever a custom kernel is passed

Line 24: `if kernel == None:`. With an array that is an elementwise comparison, so `if` raises
"truth value of an array is ambiguous". Only the default-kernel path works. Should be
`if kernel is None:`. The MATLAB twin `deconvolve.m` gets this right with `isempty`.

### B4. Both deconvolve implementations have identical if/else branches

`deconvolve.m:79-87` and `widefield_deconv.py:49-54`:

```matlab
if lag < 0
    % Negative lag: use future values
    ... x(start_idx - lag : end_idx - lag)
else
    % Non-negative lag: use current/past values
    ... x(start_idx - lag : end_idx - lag)     % identical
end
```

The conditional does nothing. Either it is correct and the branch should go, or one side was meant
to differ and the edit was never finished — the comments describe two different behaviours. Worth
confirming which was intended, because it decides the sign convention of the deconvolution.

Both also leave the first and last `max_lag` samples equal to the intercept rather than NaN, so
edge samples look like real data downstream.

### B5. `alignROIs.m` else-branch references undefined variables

Line 78 uses `areaFolder` and `areas`, neither of which exists in the function. That branch runs
when a mouse has no alignments yet, and errors. Separately, `ai` is used as both the outer
(line 54) and inner (line 61) loop index; MATLAB survives it, but it is one edit from breaking, and
`temp` (line 68) is never cleared between outer iterations.

### B6. `getSessionAlignment.m:13` calls `keyboard`

Drops into the debugger instead of erroring. In any automated run this hangs indefinitely.

### B7. `alignSessions.m:89` uses `datas{2}.mimg` inside a loop over `di`

Copy-paste leftover; harmless only because the variable is then unused. `datas{1}` at line 58 also
errors if the mouse has no widefield sessions.

### B8. `testROIalignment.m:64` plots the reference session every time

`imagesc(rfbase(:,:,3))` inside a per-session loop, instead of `data.rfData`. The QC figure shows
the same image for every session, so the check cannot fail.

### B9. `traceMaker.m:4` breaks for single-pixel ROIs

`mean(U2(Pos,:))` averages down columns. If `Pos` selects one pixel, `U2(Pos,:)` is a row vector
and `mean` averages *across components*, returning a scalar. Needs `mean(..., 1)`.

---

## C. Robustness and hygiene

| | |
|---|---|
| C1 | The tar-and-delete block is **duplicated verbatim** in `tarFrames.m` and `loadAndSVDf.m`. Two copies of the code that deletes raw data. |
| C2 | Frame-time computation is duplicated in `loadAndSVDf.m:23-34` and `checkBVW.m:21-33`, both writing the same three `.npy` files to the server. Running `checkBVW` after an SVD silently rewrites the files that SVD was built on. |
| C3 | Machine paths hardcoded in eight places and mutually inconsistent: `D:\data`, `E:\data`, `C:\Users\SteinmetzLab\…`, `C:\Users\IBL_ephys\…`, `C:\Users\Steinmetz Lab\…`, `C:\proj\Pipelines\…`, `Z:\Subjects`, `\\steinmetzsuper1…`, `\\sahale…`. The two `temp_*` scripts disagree about where `badBVWFolders.mat` lives. |
| C4 | The session list is a block of commented-out lines at the top of `tarFrames.m` and `svdScript.m`, hand-edited per run. Should be an argument or a queue file. |
| C5 | `loadAndSVDf.m` writes `E:\tempProc\{blue,violet}.dat` and never removes them — full uncompressed movies left on E: indefinitely. |
| C6 | No `try`/`finally` around the `fopen`'d `.dat` handles; an error mid-loop leaves handles open and a partial file. |
| C7 | `hemoCorrect.m` documents `nSV` as defaulting to 500 but has no default, so `hemoCorrect(path)` errors. `hemoFreq` is set twice (lines 69-70), the second silently winning — a scientific parameter changed by editing the file. |
| C8 | `hemoCorrect.m:66` takes blue timestamps from the **violet** resampled time vector (`tb = tVps(...)`). Possibly deliberate after interleaving, but undocumented and a plausible half-sample bias. |
| C9 | The known length-mismatch fix is **commented out** in `loadUVt.m:37-39`, so `t` and `V` can differ in length silently. |
| C10 | `checkBVW.m:28` flags blank frames as bad; `alignBlueVioletExposures.m:85` comments that they "aren't actually bad". Two definitions of a bad session. |
| C11 | `isdir` (deprecated) in three files; `str2num` (eval) in two; `disp(sprintf(...))` throughout instead of `fprintf`; `warning(sprintf(...))` at `loadAndSVDf.m:25`, where a `%` in the message would be reinterpreted. |
| C12 | `alignBlueVioletExposures.m:53` loops over **every timeline sample** in MATLAB — at 30 kHz over an hour that is ~10⁸ iterations. Vectorisable with `histcounts`/`discretize`. The `tic`/`toc` around it suggests this is already felt. |
| C13 | Figures created and never closed in `alignSessions.m`, `testROIalignment.m` (two per iteration), and `hemoCorrect.m` (two `svdViewer` calls per run). |
| C14 | `alignBlueVioletExposures.m:21` says "no violet frames will be created" when **blue** is off, and vice versa at line 26 — a misleading diagnostic in exactly the situation someone would be debugging. |
| C15 | Nothing records which version of the pipeline produced a session's SVD. Since A4/A5 change results, writing a git hash into `dataSummary.mat` would let you tell which sessions need reprocessing after a fix. |

---

## Proposed order of work

**Stage 0 — stop the bleeding (small, do first).**
A1 (tar the frame list, not the folder) and A2/A3 (verify before deleting). These are the only
findings that can lose data, and A1 is a one-line change.

**Stage 1 — the timestamp bug (A4, A5, A6).**
Derive per-colour timestamps from the same colour assignment the SVD used; assert the frame counts
match; stop NaN from reading as true. Then work out which existing sessions are affected —
`badBVWFolders.mat` plus a re-run of `checkBVW` over the corpus gives you the list — and reprocess
those. `wfcompress extract` makes reprocessing cheap now: it replaces `untar` in `reRunSVDf.m`
with one command, straight from the `.wfz`, no intermediate tar.

**Stage 2 — delete the dead files (B1, B2, B6-B8), fix the two deconvolve bugs (B3, B4).**

**Stage 3 — consolidation (C1-C4).**
One copy of transfer-verify-delete; session list as an argument; machine paths in one config file.

**Stage 4 — compression.** See below.

---

## Should the pipeline write `.wfz` directly instead of `.tar`?

All measured on this 16-core workstation (Samsung 970 PRO NVMe, 10 GbE to `\\sahale`) while
otherwise idle, on two real sessions — one Basler TIFF, one headerless raw.

### Short answer: yes, about the same — 52 MB/s either way

| route | rate | per TB |
|---|---|---|
| copy an **existing** tar to the share — *not what the pipeline does* | 318–363 MB/s | 0.8 h |
| **7-Zip: loose TIFFs → tar on the share** — what the pipeline does | **52 MB/s** | **5.3 h** |
| **wfcompress: → `.wfz` on the share**, 16 threads | **52 MB/s** | **5.3 h** |

The two are indistinguishable, and there is a reason rather than a coincidence. Reading a few
hundred thousand loose TIFFs costs almost exactly what JPEG-LS costs to encode them
(~19 s versus ~19 s for 1.19 GB here), and the two overlap. The encoder hides behind the disk.

**Watch out for two traps in measuring this.**

*Do not benchmark against a file copy.* The pipeline never copies a finished tar; `my7zTar` walks
a directory of individual files. Copying an existing tar runs at 318–363 MB/s and makes
compression look 6× slower than it is.

*Do not benchmark with a warm cache.* The same 7-Zip job measured **76 MB/s** after three other
passes had already walked every file, and **52 MB/s** as the first thing to touch them. The
compression numbers are CPU-bound and barely move; the tar number moves by 45%. A real session is
100–300 GB against 64 GB of RAM, so the cold number is the realistic one.

You can check this on the acquisition machine without writing anything: `tarFrames.m` already
wraps `my7zTar` in `tic`/`toc`, so the real rate for a real session is already in your console
history. If it is above ~65 MB/s, compression would become the constraint there.

### Detail

| | raw (1.61 GB) | TIFF (1.19 GB) |
|---|---|---|
| wfz → share, 16 threads | 30.3 s, 53 MB/s | 22.8 s, 52 MB/s |
| wfz local, 16 threads | 24.8 s, 65 MB/s | 18.6 s, 64 MB/s |
| wfz local, 8 threads | 29.1 s, 55 MB/s | 20.8 s, 57 MB/s |
| wfz local, 4 threads | 38.5 s, 42 MB/s | 29.5 s, 40 MB/s |
| streaming verify afterwards | 17.3 s (93 MB/s) | 12.9 s (92 MB/s) |
| compression ratio | ×2.36 | ×2.35 |

Loose-TIFF source, 1,879 frames at 632 kB each, on the share:

| | rate |
|---|---|
| 7-Zip, first pass over freshly written files | **52 MB/s** |
| 7-Zip, after three other passes had warmed the cache | 76 MB/s |
| read every file once and discard the bytes (Python) | 43–49 MB/s, ~70–78 files/s |
| bsdtar (`C:\Windows\system32\tar.exe`) | 11 MB/s |
| Python `tarfile` | 15 MB/s |

**The archiver matters enormously** — 7-Zip is 7× faster than bsdtar at the identical job, because
the others write to SMB in 8–16 kB chunks. Only the 7-Zip rows say anything about the pipeline.

### Staging the `.wfz` locally first, then copying it across

Worth a look, because writing the compressed file to the share during compression is measurably
slower than writing it locally. On the idle machine, 16 threads:

| | raw (1.61 GB) | TIFF (1.19 GB) |
|---|---|---|
| compress → local `.wfz` | 24.8 s | 18.6 s |
| then block-copy it to the share | 2.2 s (310 MB/s) | 1.6 s (314 MB/s) |
| **staged total** | **27.0 s** | **20.2 s** |
| compress → share directly | 30.3 s | 22.8 s |
| **staging saves** | **11%** | **11%** |

So yes, staging is about 11% faster — but that gap is an artefact worth understanding rather than
a property of the network. Copying the finished file runs at ~310 MB/s; the same bytes written
incrementally during compression achieve only ~110 MB/s effective. The difference is **write
size**: `compress` emits one JPEG-LS codestream at a time, 250–350 kB per frame.

Writing 768 MB to the share in different chunk sizes (measured under load, so read the ratios not
the absolutes):

| write size | share | local NVMe |
|---|---|---|
| 64 kB | 5 MB/s | 785 MB/s |
| 256 kB — *about one frame* | 14 MB/s | 945 MB/s |
| 1 MB | 40 MB/s | 507 MB/s |
| 4 MB | 69 MB/s | 614 MB/s |
| 16 MB | 131 MB/s | 934 MB/s |

**A 9× spread on the share; the local disk does not care at all.** So the fix is not to stage the
file — it is to buffer the writes, which `codec.WRITE_BUFFER` (16 MB) now does. Staging would cost
an extra 42% of local disk at exactly the moment the pipeline is trying to free it, for a gap that
buffering closes for nothing.

### Recommendation

**Transfer time is not an argument against it.** On this hardware they are the same speed. So the
decision comes down to what else changes:

**For:**
- The server never holds the uncompressed copy at all, so there is no second pass and no future
  equivalent of the 68 TB campaign now running.
- Peak space on the share drops by 2.4× immediately rather than eventually.
- You get a real integrity proof instead of the size check in A2 — every frame round-trips through
  the codec during writing, and `wfcompress check` can prove byte-identity before anything is
  deleted.

**Against:**
- It needs a **folder-of-TIFFs → `.wfz`** entry point. Today `wfcompress` only ingests a tar. This
  is the real cost of the change — maybe a day, plus testing.
- It puts a full CPU load on the acquisition machine for the duration of the transfer. If a
  recording can be running while the previous session transfers, that is an operational risk that
  tarring does not have.
- Fewer cores means slower: 8 threads gave 55–57 MB/s and 4 threads 40–42 MB/s here, against a
  tar rate that does not depend on cores at all. On a 4-core acquisition machine compression
  *would* become the constraint.

**Suggested route:** keep writing tars for now, keep compressing on a schedule afterwards
(`wfcompress.lab.batch --min-age-s 3600` already refuses archives still being written). Build the
folder→`.wfz` entry point when there is time, measure it on the acquisition machine against that
`tic`/`toc` number, and switch if it holds up. If you do switch, use **process-based** parallelism
rather than threads — the 16-thread figures above are GIL-bound, and the bulk driver reaches
86.7 MB/s on this machine with 8 processes × 4 threads, which would put compression comfortably
ahead of the tar.

### Take these regardless

- **Replace the size check with `wfcompress check`** (fixes A2/A3). It proves byte-identity against
  a hash taken while the archive was read, at about a third of the cost of re-reading both files.
- **`reRunSVDf` should read the `.wfz` directly.** `wfcompress extract <wfz> <dir>` returns the
  original TIFFs byte-for-byte in one command with no intermediate tar, which also fixes B2's
  drive-letter mismatch by replacing that code entirely.
- **`--bin` could replace the `imread`-per-frame loop.** `wfcompress extract <wfz> out.bin --bin`
  writes a flat `rows × cols × nFrames` uint16 file in acquisition order — the same shape
  `get_svdcomps` already wants for `svdOps.RegFile`. That would remove `loadAndSVDf.m`'s
  200,000-iteration `imread` loop from reprocessing entirely.
