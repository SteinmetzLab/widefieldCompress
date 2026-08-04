# Code review: `widefieldCompress`

Reviewed 2026-08-03. This review covers the core codec and container, random-access reader,
laboratory census and batch workflow, tests, checked-in data artifacts, documentation, and
benchmark methodology.

## Executive assessment

This is a thoughtful and promising prototype with unusually good attention to lossless pixel
verification. The JPEG-LS choice is plausible, the low-bit transform is carefully checked per
frame, and the real-data pilots provide useful evidence. However, the repository is not yet safe
enough to serve as the basis for deleting the approximately 120 TB source corpus.

The principal blockers are:

1. non-atomic output and the possibility of truncating the input;
2. incorrect temporal semantics for `frame-N` archives;
3. tar edge cases that contradict the unconditional byte-identical claim;
4. no protection against a source archive changing during compression;
5. assuming that every TIFF has the first TIFF's pixel layout;
6. footer and memory-scaling risks for very large sessions.

The default batch workflow's final streaming verification mitigates some of these problems, but
the core API and CLI can still produce misleadingly authoritative metadata or sidecars, and several
risks are not detectable by the current verification design.

## Prioritized findings

### [P0] Compression or decompression can truncate its own input

`compress()` opens the final destination directly with `open(dst, "wb")` without first checking
whether `src` and `dst` identify the same file (`src/wfcompress/codec.py:145`). If the paths are the
same, the source is truncated before the sequential compression read. A different pathname that is
a hardlink or symlink alias has the same problem.

`decompress()` has the equivalent problem at `src/wfcompress/codec.py:314`. It reads the metadata,
opens the destination for truncation, and only then starts executing the reconstruction generator.
If the two paths alias, it destroys the `.wfz` before reconstruction.

Output is also non-atomic. A crash, disk-full error, SMB interruption, late low-bit violation, or
footer exception leaves a partial file under the final `.wfz` or `.tar` name. If old sidecars are
already present, they can falsely describe the partial replacement as valid.

Recommendation:

- reject destinations for which `os.path.samefile(src, dst)` is true, with a resolved-path fallback
  for a destination that does not exist;
- write to a unique temporary file in the destination directory;
- close, flush/fsync where meaningful, and fully verify that temporary file;
- atomically replace the final destination only after verification;
- define and test an explicit policy for an already-existing destination.

### [P1] `WfzReader` exposes the wrong temporal order for `frame-N` archives

The repository's inventory establishes that `frame-N` members are stored in lexicographic name
order, for example `frame-0, frame-1, frame-10, frame-100, ...`, rather than acquisition order
(`docs/PLAN.md:72-78`; `scripts/scratch.py:24-40`).

The compressor selects data-bearing entries in archive order (`src/wfcompress/codec.py:96-97`) and
writes codestreams in that same order (`src/wfcompress/codec.py:156-168`). `WfzReader.frame(i)` then
treats index row `i` as scientific frame `i` (`src/wfcompress/reader.py:64-74`). Consequently,
`frame(2)` can return acquired frame 10, not acquired frame 2. The blue/violet sequence is likewise
not correctly represented by alternating reader indices.

This contradicts the container documentation's statement that codestreams are in temporal order
and the design plan's decision to reorder frames temporally. Byte-identical tar reconstruction can
still be preserved by storing a mapping in the footer.

Recommendation: parse and validate the numeric suffix for `frame-N` members, store codestreams in
temporal order, and record both temporal-to-tar and tar-to-temporal mappings. Alternatively, preserve
archive order but make that explicit in the API and add a separate temporal lookup. Given the
scientific use case, temporal order should be the default.

### [P1] The unconditional byte-identical claim does not cover all tar layouts the parser accepts

Reconstruction emits member alignment padding as newly synthesized zero bytes
(`src/wfcompress/codec.py:288-290`) rather than preserving the source padding. The compressor hashes
the original padding but does not store it.

The reconstruction loop emits zero-size tar entries only while searching for the next data-bearing
entry (`src/wfcompress/codec.py:278-287`). A directory, empty file, or other zero-size entry after the
last data member is stored in `tarheaders.bin.zst` but never emitted; reconstruction jumps directly
to the trailer.

Targeted in-memory checks demonstrated both failures:

- Python's standard tar reader accepted an archive whose member padding contained a nonzero byte,
  but reconstruction with zero padding had a different SHA-256;
- a tar containing a frame followed by a directory entry had a different SHA-256 when that final
  directory header was omitted.

Default batch `verify()` catches the final hash mismatch. Core `compress()`, however, has already
written `"byte_identical_restore": true` (`src/wfcompress/codec.py:203`), and the CLI writes a README
that states the archive can be rebuilt byte-for-byte without automatically performing the full
check.

Recommendation: model the tar as an exact sequence of header, body, and padding bytes, retaining
all non-pixel bytes. At minimum, reject nonzero member padding and unsupported trailing entries
during preflight, before writing a codestream. Do not assert byte-identical restoration until the
whole reconstructed-tar hash has been checked.

### [P1] A changing source can yield a self-consistent but nonexistent hybrid snapshot

Compression performs several separate views of the source:

1. `read_entries()` reads headers;
2. `_detect_shift()` reopens and samples members;
3. the encoding pass reopens and streams spans;
4. `trailing_bytes()` opens the file again for the trailer.

There is no source lock, finalized-session marker, minimum age, or before/after identity check. If
pixel data changes in place while compression progresses, the output can contain frames observed at
different times. The stored source hash is the hash of that sequence of reads, which may not match
any complete state the source archive ever had.

Streaming `verify()` cannot detect this: it proves consistency with the hash accumulated during the
compression pass, not with a stable final version of the original. Simple appends are likely to be
caught by the size check, but same-size writes need not be.

Recommendation: process only explicitly finalized sessions; record and recheck stable file identity,
size, high-resolution modification time, and preferably a completion marker before and after the
operation. Before destructive migration, rehash the unchanged original or obtain a filesystem
snapshot/lock with appropriate consistency guarantees.

### [P1] Only the first TIFF's layout is identified

The first member supplies shape, dtype, pixel offset, and byte count
(`src/wfcompress/codec.py:101-105`). All later members are split using that same `FrameLayout`.

The per-frame `join(split(raw)) == raw` test proves that the selected byte region can be removed and
restored. It does not prove that the selected region is the later TIFF's actual image data. A later
TIFF with a different image-data offset but the same total shell length could round-trip exactly
while `WfzReader` returns some header/table bytes as image pixels.

The current varying-shell tests vary description contents or reject unequal shell sizes, but do not
exercise independently valid TIFFs with differing offsets/layouts.

Recommendation: inspect every TIFF's structural layout and require it to match the first, or store
layout information per frame. Also explicitly require unsigned 16-bit samples rather than accepting
any two-byte TIFF dtype and interpreting it as `uint16`.

### [P1] Footer design creates a session-wide failure point and high memory peaks

The only index and reconstruction metadata live in one ZIP footer referenced by one eight-byte
offset. Corruption or truncation of the offset, ZIP directory, `index.npy`, or compressed headers
can make an entire session inaccessible even when all JPEG-LS codestreams remain intact. The
per-frame CRCs cannot help if their index is unavailable.

`read_footer()` reads the entire remainder of the file and inflates all tar headers and shells into
memory (`src/wfcompress/container.py:84-98`). `WfzReader` incurs that cost just to read one frame
(`src/wfcompress/reader.py:26-34`). The sidecar preview path loads the footer through `WfzReader` and
then reads it again to retrieve a shell (`src/wfcompress/sidecar.py:120-145`).

The largest checked-in census record has roughly 680,000 frames. Its raw 512-byte headers alone are
about 348 MB, before Python objects, joins, indexes, ZIP buffers, and decoding batches. If 4.6 KB
shells differ per frame, the `shell_pool` can exceed 3 GB. Eight concurrent processes multiply these
peaks.

Recommendation:

- stream-compress headers and shells instead of building giant concatenations;
- avoid retaining `shell_ids` when shells are uniform;
- detect unsupported varying shell lengths immediately rather than after encoding the whole file;
- read metadata and the index lazily without inflating reconstruction blobs for random frame access;
- use positioned reads or independent handles for thread-safe random access;
- duplicate or checkpoint the index and document a recovery procedure for a damaged footer.

### [P2] Shift detection can silently lose compression and records sampled metadata as fact

`_detect_shift()` samples at most 400 frames. If a rare low bit is present in a sampled frame, the
detected shift can fall to zero and silently forfeit much of the expected compression. If the sample
overestimates the shift, encoding eventually aborts when a violating frame is encountered, which
may happen very late in a hundreds-of-GB archive.

`payload_bits` is also computed from the sampled OR mask. A bright value in an unsampled frame can
make the recorded payload width inaccurate even though compression remains lossless.

Recommendation: aggregate the OR mask over every frame during encoding and report the actual final
payload width. Define anomaly thresholds against expected camera configurations. Consider a cheap
full first pass, or a restart strategy, when shift zero would materially expand the output.

### [P2] Decompression does not validate the recorded whole-tar hash

`decompress()` exhausts the reconstruction stream and checks the pixel hash indirectly, but does
not hash the reconstructed tar or compare it with `source_tar_sha256`
(`src/wfcompress/codec.py:302-323`). It reports only output size and pixel-hash success. Since every
byte already passes through the loop, computing the whole-tar SHA-256 would add little cost and
would catch damaged reconstruction metadata as part of decompression itself.

### [P2] Batch resume and census completeness are too trusting for deletion decisions

Batch resume treats any prior JSONL row with `ok` as sufficient to skip that source path
(`src/wfcompress/lab/batch.py:110-120`). It does not check whether the output still exists, whether
its current hash matches a receipt, whether `wfcompress check` still passes, or whether the source
has changed since the old record.

The census walker silently discards `OSError` while traversing directories
(`src/wfcompress/lab/census.py:110-125`). A transient SMB or permission failure can omit an entire
subtree without producing an error record, undermining claims about corpus completeness.

The checked-in `data/tar_census.csv` is also an older schema: it starts with `tag` and lacks the
current reader's required `server`, geometry, shift, SVD, and error fields. It cannot directly drive
the current `Census.read_csv` implementation.

Recommendation: bind completion records to source and output fingerprints, revalidate outputs when
resuming, report traversal failures explicitly, retry transient SMB failures, and version all CSV
schemas.

### [P2] Format longevity and provenance are not yet archival-grade

The writer records format version 1, but readers do not validate or dispatch on the version. Python
dependencies have only lower bounds, there is no lockfile or reference environment, and the footer's
decompression instruction installs the repository's current main branch rather than the exact
compatible revision. Dependency versions, especially `imagecodecs`/CharLS, are not recorded.

The format is intended to replace data retained for many years, so it should have:

- a standalone byte-level specification;
- explicit compatible-version handling;
- pinned source/release artifacts and dependency provenance;
- small golden `.tar`/`.wfz` fixtures with fixed hashes;
- an independent recovery/decoder implementation or at least a minimal dependency-light extractor.

### [P3] Reader and sidecar details

- `WfzReader` has one shared seekable file handle and is not safe for simultaneous calls from
  multiple threads.
- `iter_tar_bytes()` claims to yield a final `("__pixels__", digest)` sentinel, but implements no
  such sentinel (`src/wfcompress/codec.py:234-299`).
- The footer's `meta.json` is serialized before `output_bytes`, `footer_bytes`, `ratio`, and
  `elapsed_s` are added to the returned metadata (`src/wfcompress/codec.py:224-230`). Consequently,
  the supposedly authoritative internal metadata lacks fields present in the sidecar receipt.
- Selecting a preview by maximum pixel value can choose a hot-pixel or saturated artifact rather
  than a representative frame. A robust brightness statistic would be preferable.

## Benchmark and scientific-method review

The JPEG-LS choice is reasonable, but several conclusions are stronger than the evidence.

### Limited sampling

The principal codec shootout uses 64 frames from one 560x560 session
(`docs/BENCHMARKS.md:7-26`). Broader comparisons cover only a few sessions. There are no repeated
runs, confidence intervals, randomized frame positions, or complete hardware and library-version
records. First-frame subsets can be unrepresentative because illumination startup, bleaching,
motion, and signal variance change during a recording.

The eight-session, 119.5 GB byte-identical pilot is valuable end-to-end evidence, but it is still
small relative to approximately 1,600 archives and does not exercise the largest 430 GB sessions.

### Benchmarks are not independently reproducible

Several scripts use:

- an untracked `sample_frames.npy`;
- hard-coded `Y:` session paths and local `D:` paths;
- `sys.path` entries pointing into a temporary Claude scratch environment;
- unrecorded codec/library versions.

The scripts therefore cannot regenerate the published tables from a clean checkout. Inputs should
be parameterized, exact sampled frame names/indices should be recorded, and sanitized fixtures or a
manifest with source hashes should be provided.

### Transform overhead is omitted

The mean-image experiments compare only JPEG-LS residual codestream sizes. They exclude the mean
images and offsets needed for inversion. A single session-wide mean has modest amortized cost, but
a new full-resolution mean every 40 frames costs roughly one image per 40 source images and can
easily exceed the reported additional 0.4% compression gain. On the demonstrated 120-frame sample,
this omitted side information is particularly significant.

### Some comparisons are not like-for-like

Generic compressors operate on a whole multi-frame block and can exploit cross-frame redundancy,
whereas JPEG-LS/JPEG-XL are encoded independently per frame to preserve random access. Container,
index, and transform-metadata costs are generally omitted. One early zstd CLI benchmark uses all
threads (`-T0`) despite documentation describing throughput as single-core. Warm-up, cache effects,
and repetitions are not controlled.

In `scripts/bench2.py`, byte-shuffled zstd runs do not execute the inverse transform, despite the
documentation saying all measurements are round-trip checked. Its reporting also uses the full
64-frame `raw` size when printing separate blue-only and violet-only results, which overstates those
individual ratios and speeds.

### The stated noise floor is not an information-theoretic bound

The per-pixel temporal standard deviation calculation assumes Gaussian discretized noise and then
averages marginal differential-entropy estimates. It ignores spatial and temporal correlation,
heteroscedastic shot noise, quantization details, and the distinction between biological signal and
noise. The documentation acknowledges that it is heuristic and that a codec can beat it, but then
uses proximity to it as evidence that no substantial gain remains. That conclusion is suggestive,
not established.

More defensible language would be: JPEG-LS is close to the best tested methods on the sampled data,
and additional engineering complexity has not yet shown a compelling benefit.

## Testing assessment

The 25 tests cover several valuable cases:

- TIFF and headerless round trips;
- shift zero and shift four;
- non-square geometry and refusal to guess;
- uniform and varying equal-length TIFF shells;
- pixel and whole-tar verification;
- basic payload corruption;
- preview generation;
- separation of the reusable core from lab-specific paths.

Important missing cases include:

- source and destination aliasing;
- interruption, disk-full, atomic replacement, and existing destinations;
- nonzero tar padding and zero-size entries after the final frame;
- PAX/GNU extensions, long names, invalid checksums, truncated headers, and unusual size encodings;
- mixed per-frame TIFF offsets, shapes, byte orders, or sample types;
- numeric temporal ordering for `frame-N`;
- a source changing during compression;
- large-index/footer memory behavior;
- malformed or internally inconsistent footer metadata;
- batch resume, transient SMB errors, and census-schema compatibility;
- format-version compatibility and golden archived fixtures.

The corruption tests accept any `Exception`, which can allow them to pass for reasons unrelated to
the intended integrity check. They should assert specific exception types/messages and verify that
no final output was committed.

## Positive design choices

Several parts are particularly good and should be retained:

- JPEG-LS is run in lossless mode and decoded immediately for every encoded frame.
- The low-bit transform is reversed and compared per frame.
- Per-frame reassembly is checked byte-for-byte.
- The source archive and pixel streams have SHA-256 digests, with CRC32 for targeted codestream
  corruption detection.
- Headerless geometry is refused rather than guessed by default.
- Site-specific census/batch behavior is separated from the reusable core and tested as such.
- Original deletion is not automated by the current batch tool.
- Real pilot archives from both supported storage flavors have been restored and independently
  hash-compared.

## Recommended release gates before deleting originals

1. Fix same-file destruction and use verified atomic temporary outputs.
2. Define and test numeric temporal semantics for `frame-N` direct access.
3. Preserve every tar byte or explicitly reject unsupported tar structures during preflight.
4. Ensure source archives are finalized and stable throughout compression.
5. Validate every TIFF's true layout.
6. Make full whole-tar verification part of successful compression/decompression, not an optional
   follow-up assertion.
7. Add golden fixtures and the missing adversarial tests, then run them in CI on supported Python
   versions.
8. Stress-test the largest observed frame count and eight-process memory peak.
9. Version census/log schemas and make resume revalidate both source and output.
10. Run a larger stratified pilot, including the largest archives and all acquisition eras, before
    any deletion tranche.

## Validation status

Validation was run in a disposable Python 3.12.13 virtual environment with the project installed in
editable mode and its `dev` extra. The resolved principal versions were:

- NumPy 2.5.1;
- imagecodecs 2026.6.26;
- tifffile 2026.7.31;
- zstandard 0.25.0;
- pytest 9.1.1;
- Ruff 0.16.1.

`pytest -q -p no:cacheprovider --basetemp <writable-temp>` completed successfully: **27 tests
passed in 2.63 seconds**. The explicit base-temp was required because the managed review session
could not access pytest's pre-existing default user-temp/cache directories; that was an execution
environment issue, not a repository test failure.

Ruff completed but the repository is not lint-clean: **50 findings** total, comprising 42 in
`scripts`, four in `src`, and four in `tests`. Most are formatting, unused-import, or modernization
issues. The more meaningful lint findings include the two blind `pytest.raises(Exception)` checks
already discussed and a loop-variable closure warning in `scripts/bench2.py`. No automatic Ruff
fixes were applied.

Static compilation of `src`, `tests`, and `scripts` also succeeded under Python 3.14. Targeted
in-memory tar probes reproduced the padding and trailing-zero-size-entry issues described above.
The passing existing suite does not contradict those findings because it contains no tests for
those tar layouts, temporal reordering, source/destination aliasing, or source mutation.
