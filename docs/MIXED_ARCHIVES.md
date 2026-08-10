# The 21 `widefield.tar` files that are not only widefield

> **Update, 2026-08-10 — all 21 have already been trimmed, by someone outside this project.**
> Between 2026-08-06 15:10 and 2026-08-10 01:50 every one of them was rewritten to contain only
> the frames. Together they went from **3.36 TB to 2.13 TB**, reclaiming 1.23 TB, and each one's
> frame count now matches the break point this document recorded, exactly — e.g.
> `FD_011/2026-02-25` holds 243,808 frames and nothing else.
>
> Checked afterwards: our own audit log (`data/fileEditLog.csv`, 1,171 rows) contains **no `.tar`
> writes at all**, so the rewrite was not done by this tooling; and **all 21 ephys recordings still
> exist unpacked on the share at their recorded sizes**, so nothing was lost.
>
> Consequence: these are now ordinary uniform frame archives and the normal pipeline compresses
> them with no special handling. The ones that already failed in the current run will be picked up
> on the next restart. The frames-only mode described at the end of this document was built anyway
> and is kept, because `tarFrames.m` still tars the whole session directory (see
> [PIPELINE_REVIEW.md](PIPELINE_REVIEW.md) A1) and will keep producing mixed archives until it is
> fixed.

Found while diagnosing two bulk-run failures that looked like corruption
(`ValueError: invalid literal for int() with base 8`). They are not corrupt. They contain more than
they claim to.

## What they are

A normal `widefield.tar` is one uncompressed camera frame per member, every member the same size.
Twenty-one of the 1,120 on `Y:` are not: they hold the widefield frames **and then a whole
SpikeGLX recording** — `p0_g0/`, `p0_g0_imec0/`, a `.ap.bin` of 10–82 GB, its `.ap.meta`, and
sometimes `p0.missed_samples.imec0.txt`. Frames always come first.

The `ValueError` was a symptom of exactly this. An imec `.ap.bin` is over 8 GiB, which does not fit
in tar's 11-octal-digit size field, so GNU tar switches to **base-256 binary** encoding. The parser
only read octal. Fixed in `tarwalk.parse_size`; the archives are now refused with a clear message
instead of crashing.

## Scale

| | archives | size |
|---|---|---|
| uniform frame archives | 1,099 | 117.31 TB |
| **mixed session archives** | **21** | **3.36 TB** |
| probe errors | 0 | — |

Within the 3.36 TB:

| | | |
|---|---|---|
| widefield frames | **2.13 TB** | 63 % |
| the bundled `.ap.bin` | **1.21 TB** | 36 % |
| headers, meta, padding | 14.8 GB | <1 % |

Ten subjects, concentrated in `FD_011` (7 sessions), `FD_004` (3), `ZYE_0098`, `AL_0023`, `FD_012`
(2 each). Full list with byte offsets: [`data/mixed_archives_detail.csv`](../data/mixed_archives_detail.csv).

## The bundled ephys is a duplicate, not the only copy

Checked directly ([`data/mixed_ephys_elsewhere.csv`](../data/mixed_ephys_elsewhere.csv)):

- **21 of 21** have the same recording sitting unpacked on the share outside the tar.
- **21 of 21** match to the byte — e.g. `FD_011/2026-02-25` holds 82,259,281,720 B inside the tar
  and `1/p0_g0_imec0/p0_g0_t0.imec0.ap.bin` on disk is 82,259,281,720 B.
- **16 of 21** additionally have a Kilosort 4 output derived from the on-disk copy.

Two mismatch in *location* while matching in size, which is worth knowing before anyone deletes
anything: `AL_0041/2026-04-22` has the tar in session `1` naming `p2_g0`, while the loose copy is
under session `3`; `AL_0023/2023-10-27` sessions 1 and 4 each name their own probe. Same data,
filed differently.

So nothing is lost if the tar-embedded ephys is never recovered. Deciding what to *do* about the
duplication is a lab call, not a compression one.

## What the tool does about them today

Refuses them, clearly, and moves on. `_preflight` requires a constant member size, which these
violate, so they are skipped without writing anything.

## What supporting them would cost and gain

The container would need to store non-frame members verbatim (compressed with zstd, or copied) in
addition to the JPEG-LS frame payload. That is a real format change, not a flag.

**Gain: ~1.23 TB**, from compressing the 2.13 TB of frames at the measured 2.37×. Against a
campaign total near 68 TB, that is under 2 %.

**Cheaper alternative**, if the lab confirms the embedded ephys is redundant: repack those 21 tars
to drop the ephys members, then compress normally. That recovers the same 1.23 TB *plus* the
1.21 TB of duplicated ephys — but it rewrites archives rather than only adding files beside them,
which is a different risk category and would want its own plan.

No action has been taken on any of the 21.
