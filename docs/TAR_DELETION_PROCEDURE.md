# Deleting the original tars: the procedure, the checks, and what each costs

Written 2026-08-17, updated 2026-08-18. The tool is `scripts/delete_tar.py`; the tests are
`tests/test_delete_tar.py` (24 of them, all passing).

> ## The first deletion happened: `test/2026-02-17/1`, 2026-08-18 16:33 UTC
>
> 1.19 GB tar, all 11 conditions re-verified 40 minutes beforehand (181 s), authorized by Nick.
> Recorded in `data/deletions.jsonl` and `data/fileEditLog.csv`. The `.wfz`, its receipt, README
> and frame sidecar all remain.
>
> **Three recovery paths exist for it right now, and two are already proven byte-for-byte:**
>
> | path | status |
> |---|---|
> | rebuild from the `.wfz` on the server | **proven** — C6 passed, hash matches |
> | rebuild from the `.wfz` in B2 | **proven** — C9/C10 passed, hash matches |
> | the tar itself out of a local ZFS snapshot | **proven** — see below |
> | the tar itself out of B2's hidden prior version | pending the next sync; run `offsite` |
>
> The ZFS route, verified on the box immediately after the deletion:
>
> ```
> $ sha256 -q /mnt/data/data/.zfs/snapshot/auto-2026-08-18_00-00/Subjects/test/2026-02-17/1/widefield.tar
> c14f7eedb58a36ef873be59f18dff9cc7483d503b907f6c9e80e062d1148f965      # == source_tar_sha256
> 6.47 real
> ```
>
> Six and a half seconds, locally, no Backblaze involved. **Note the directory name carries a time
> suffix** — `auto-2026-08-18_00-00`, not `auto-2026-08-18`; getting that wrong returns a bare
> "No such file or directory" that reads like the snapshot is missing.
>
> ### Three more deleted 2026-08-20/21, spanning the size range
>
> | | session | tar | result | checking time |
> |---|---|---|---|---|
> | small | `AL_0033/2025-02-24/3` | 17.47 GB | **11/11 measured** | 42 min |
> | medium | `ZYE_0091/2024-07-10/1` | 161.43 GB | **11/11 measured** | 6.5 h |
> | large | `ZYE_0046/2021-11-27/2` | 430.10 GB | **10 measured + C10 derived** | 14.0 h |
>
> Four tars deleted in total, **0.610 TB**, all in `data/deletions.jsonl`. The small one is one of
> the three `3.tar`-style archives, so it validates the path fix end to end on real data.
>
> C10 on the large archive was derived rather than measured: decoding B2's `.wfz` locally needed
> 212 GB against 104 GB free. C9 proved those bytes byte-identical to the server's copy and C6
> proved the server's copy rebuilds the recorded hash, so the conclusion follows — but it is
> reported as `DERV`, not `PASS`.
>
> Throughput, all with the compression campaign competing for the share: SMB reads 49-52 MB/s,
> B2 streaming 27-31 MB/s. The 430 GB archive needed 50,312 s of checking.
>
> **`offsite` run 2026-08-19 15:46 UTC: no hide marker yet, and that is expected.** B2 still shows
> one live version of `subjects/test/2026-02-17/1/widefield.tar` from its original 2026-02-22
> upload. Why:
>
> - the `Subjects` task is scheduled `hour 22, minute 0` **sahale-local**, i.e. 05:00 UTC;
> - the deletion happened 08-18 16:33 UTC, *during* a run that had already walked the tree, so the
>   first run able to see it started 08-19 05:00 UTC;
> - that run was **still going** at 15:46 UTC, 10.8 h in, with the lexical walk only at
>   `subjects/AL_00xx`. Within `subjects/`, `test/` sorts near the very end — lowercase follows
>   uppercase in ASCII, so after all the `AL_`/`FD_`/`JRS_`/`SM_`/`ZYE_` prefixes.
>
> So the walk has simply not reached it. Re-run `offsite` once this run finishes. If two full runs
> pass with no hide marker, that is when to get suspicious.
>
> The deletion is unaffected: the tar is still absent from the live tree, and the ZFS and `.wfz`
> recovery paths above are proven independently of B2.

This is the first delete path in the project. It is deliberately a separate script from
`wfcompress`, which still has none: the compressor can never delete, and the deleter can never
compress.

---

## The proposed first deletion: `test/2026-02-17/1`

| | |
|---|---|
| tar | 1.19 GB (`1,189,092,864` bytes) — the smallest of any `SAFE` session |
| `.wfz` | 0.51 GB, format v2, receipt claims byte-identity |
| code path | `shift=4`, big-endian TIFF, 560x560 — **the majority path**, 193 of 212 `SAFE` sessions |
| subject | literally named `test`; the lowest scientific stakes available |
| tar written | 2026-02-17, so **not pinned** by the old ZFS snapshots (see below) |
| gate | **all 11 conditions pass**, 300 s of checking, 2026-08-17 |

Why not a real subject for the first one: `ZYE_0035/2021-07-17/1` was the obvious alternative and
is a better *scientific* representative, but its directory contains a `p0_g0` ephys recording, so
it is a live experimental session. For the first exercise of a brand-new delete path, a session
whose subject is called `test` is the right blast radius. Once the mechanism is demonstrated
end to end, move to real sessions.

## PROVEN END TO END, 2026-08-28: the undo path is demonstrated, not assumed

The question this project opened with - can a deleted tar actually be recovered from Backblaze -
is now answered by observation rather than argument.

```
  PASS  P1  the tar is gone from the server
  PASS  P2  B2 shows a hide marker plus a retained prior version
  PASS  P3  the retained version restores to source_tar_sha256
  The undo path is demonstrated, not assumed.
```

`test/2026-02-17/1`, deleted 2026-08-18. Its B2 object now carries **two versions**: a `hide`
marker stamped 2026-08-28 14:37:42 UTC, and the original `upload` from 2026-02-22 retained beneath
it. Downloading that retained version gives 1.19 GB at 137 MB/s, hashing to `c14f7eedb58a...` -
exactly the `source_tar_sha256` recorded when the archive was compressed. Confirmed the same way on
`ZYE_0008/2020-07-25/1`.

**The cause of the ten-day delay is confirmed by the fix working.** The prediction was that
deletions would propagate as soon as the compression campaign stopped creating `.wfz.partial-*`
files mid-run. The campaign was stopped 08-27 05:27 UTC; a sync run started 08-28 07:37 UTC over a
stable tree, reached its delete phase about seven hours in, applied every pending deletion at
14:37, and exited. **No config change, no admin, no intervention.** The `*.partial-*` exclude is
still worth having, but only to stop this recurring next time a campaign runs.

### What this settles

- **SYNC mode works** - deletions do reach B2.
- **The 30-day window is real** - the retained version is there and restorable.
- **Gate condition 7 means what it says**: an archive whose `.wfz` is offsite can have its tar
  deleted and recovered.
- All **44 tars deleted so far** now carry hide markers; their B2 storage clears about 2026-09-27.

## Corrected 2026-08-21: the claim "this sync never deletes" was overstated

I wrote below that destination deletions appear never to happen. **Two checks prompted by Nick
undercut that, and one of them reverses it.**

### The timing evidence was weaker than I presented

rclone builds its source listing at the start of a run, so a file deleted *mid-run* was present
when the listing was taken and will not be considered for deletion until the *next* run. Run
69940 ran **2026-08-18 05:00:01 → 2026-08-21 06:20:49 UTC** (73.3 h; start confirmed twice — `ps`
reported `START=Mon22`, and its `ELAPSED 2-10:39:39` observed at a known clock time back-computes
to 05:00:01).

| deleted | when | relative to run 69940 |
|---|---|---|
| four `.wfz.partial-*` | 08-18 00:04:51 UTC | **4.92 h before it started** |
| `test/2026-02-17/1` tar | 08-18 16:33 UTC | during |
| `AL_0033/2025-02-24/3` tar | 08-20 18:01 UTC | during |
| `ZYE_0091/2024-07-10/1` tar | 08-21 01:11 UTC | during |
| `ZYE_0046/2021-11-27/2` tar | 08-21 16:12 UTC | after it ended |

**Only the four partials were deleted before a run that has since ended.** The three tars I cited
as corroboration were all deleted mid-run and prove nothing. That was an error.

### And the corpus says deletions do propagate

If the sync had never applied a deletion, B2 would have accumulated objects with no local
counterpart. Checking every live object under three whole subject prefixes spanning 2020-2026:

```
subjects/AB_0026/     271 live objects,  0 absent locally (0.0%)
subjects/AL_0048/    1998 live objects,  0 absent locally (0.0%)
subjects/SM_0001/    2816 live objects,  0 absent locally (0.0%)
subjects/test/2026-02-17/  52 live objects, 1 absent locally  <- our deleted tar, pending
```

**5,085 objects, not one stale.** Over six years of ordinary lab activity that is hard to explain
if deletions never propagated. Caveat, and it is a real one: I cannot show those three prefixes
ever *had* a local deletion, so this is strong circumstantial evidence rather than proof.

### Where that leaves it

The four partials remain a genuine anomaly — deleted 4.92 h before a 73-hour run that has since
ended, still live in B2. Either that run did not reach its delete phase (killed, or cut off when
the scheduler cycled), or rclone skipped deletions because the run had IO errors, which it does
deliberately rather than act on a possibly-incomplete listing.

**The decisive test needs no action from anyone.** Run 11957 started 2026-08-21 06:20:49 UTC,
after the partials and after three of the four tar deletions. When it ends, re-check:

```powershell
python scripts/delete_tar.py --bucket sahalebackup offsite test/2026-02-17/1
```

Hides appearing means the machinery works and this was only ever timing. Nothing appearing, on a
second completed run, is the real signal — and then the task log is the only way further.

**Do not quote "the sync never deletes" as established.** It is not.

## The original reasoning, now qualified by the above

Established 2026-08-20, and it is the thing that matters most on this page.

Four of our own temporary files were uploaded to B2 in full, then deleted locally:

| object in B2 | size | deleted locally |
|---|---|---|
| `AL_0037/2025-02-11/4/widefield.wfz.partial-5092` | 25.95 GB | 2026-08-18 00:04:51 UTC |
| `ZYE_0052/2021-12-18/2/widefield.wfz.partial-50728` | 4.08 GB | 2026-08-18 00:04:51 UTC |
| `ZYE_0087/2024-08-19/1/widefield.wfz.partial-50096` | 30.54 GB | 2026-08-18 00:04:52 UTC |
| `ZYE_0066/2022-10-25/6/widefield.wfz.partial-45068` | 2.27 GB | 2026-08-18 00:04:53 UTC |

Sizes match the `fileEditLog.csv` delete rows exactly, and all four are absent from the server now.
The rclone run began 2026-08-18 05:00 UTC — **five hours after those deletions** — and its walk has
since passed `AL_` and reached `FD_`. Yet B2 still lists all four as live `upload` objects with
**zero hide markers**, 2.5 days later.

**So destination deletions are not applied as the walk passes a directory. They are deferred to the
end of the run** (rclone's `--delete-after` behavior; the task sets no `--delete-*` flag). And the
run has not ended in 58+ hours.

Two consequences:

1. **The `test/2026-02-17/1` hide marker was never going to appear on the timescale I predicted.**
   My earlier explanation — that the lexical walk had not yet reached `test/` — was incomplete and
   partly wrong. Even a session the walk *has* passed does not get deleted remotely mid-run.
2. **Deleting tars reduces the Backblaze bill only once a sync run completes a pass.** Until then
   the tar objects stay live and billed, on top of the `.wfz` that replaced them. Local pool space
   still comes back after the 31-day ZFS window, but the B2 saving waits on the sync.

That makes the sync problem in `B2_THROUGHPUT.md` **not a separate concern** — it is directly on the
critical path to the cost saving the deletions are for. Nothing about deletion *safety* changes.

Incidentally this also puts a number on the `--exclude *.partial-*` recommendation: 62.8 GB of pure
temp-file garbage is sitting in B2 as live objects right now, and will stay until a run finishes.

## The cheap sweep over the whole corpus, 2026-08-20 — and the bug it caught

`python scripts/delete_tar.py --bucket sahalebackup sweep` runs only the cheap conditions
(C1-C5, C7) across every archive in the run log. 466 archives in about 6 minutes with 12 workers,
writing `data/cheap_sweep.csv`. It deletes nothing and has no delete path.

```
  PASS   395
  REFUSE  71
     70  failed ['C7']   - the .wfz is not in B2 yet, i.e. the upload backlog
      1  failed ['C4']   - test/2026-02-17/1, whose tar we deleted on 08-18
```

**Zero archives fail any local integrity condition.** No missing or wrong-sized `.wfz`, no missing
receipt, no format-v1 file, no run-log/receipt hash disagreement, and no unexplained missing tar.
The only refusals are the backlog and our own deletion.

### The bug it caught: three archives are not called `widefield.tar`

The first sweep flagged three archives as **"the tar is already gone"** plus a missing `.wfz` and
no receipt — which reads like data loss. It was not. Those three are named after the experiment
number, and the run log records it:

| session | actual files |
|---|---|
| `AL_0033/2025-02-24/3` | `3.tar`, `3.wfz` |
| `test/2025-11-05/1` | `1.tar`, `1.wfz` |
| `Subjects/test/2025-11-04` | `1.tar`, `1.wfz` (directly under the date, no experiment subdir) |

463 of 466 are `widefield.tar`; these three are not. **`delete_tar.py` had the filename
hardcoded**, so it looked in the wrong place and refused.

It failed in the safe direction — a false refusal, never a wrong deletion — but it would have
silently excluded those three from the campaign forever, and the alarming wording would have sent
someone hunting for lost data. Fixed by taking every path from the run log's own `tar` and `wfz`
fields rather than composing a filename, with `server_path()` mapping the recorded `Y:\...` form
onto the UNC share and `b2_key()` deriving the object key from the same recorded path. Four
regression tests cover it, including a fixture whose files are named `3.tar`.

**The general lesson for this project: the run log is the authority on where a file is.** Anything
that reconstructs a path from a session name will be wrong for some fraction of the corpus.

## Is the offsite hide-marker check a gate? No — and the distinction matters

Asked 2026-08-20, when the check had gone three days without completing because the sync is 58 h
into a single pass. The answer is that **it was never a data-safety gate; it is a cost check**, and
conflating the two would stall the project for no gain.

What deletion safety actually rests on, all proven **per archive at delete time**:

| | |
|---|---|
| C6 | the `.wfz` on the server rebuilds the original tar, today |
| C8 | the tar being deleted is the exact bytes that hash was taken over |
| C9 / C10 | the `.wfz` in Backblaze is byte-identical and independently rebuilds the tar |
| ZFS | the tar itself sits in a daily snapshot for 31 days — hash-verified on 08-18 |

None of those involve the hide marker. Now the two branches if we delete without checking it:

- **Propagation works** (overwhelmingly likely): B2 hides the tar, bills 30 more days, then stops.
  The goal is met.
- **Propagation fails**: the tar object stays live in B2 and keeps billing. We find out and remove
  it B2-side with a write-capable key. **A cost problem, fully recoverable, and no data is at
  risk in either branch.**

The evidence that it works is already strong without the test: `transfer_mode: SYNC` in the
TrueNAS config, the running rclone command line literally reads `sync /mnt/data/data/Subjects
remote:sahalebackup/subjects`, and the `ZYE_0098/2025-12-17/2` case shows a locally-trimmed tar
propagating to B2 with the prior version retained. The only thing unobserved is a *hide* marker
specifically, and destination-side deletion is `rclone sync`'s defining behavior.

**And the check is self-verifying.** Every tar we delete produces a hide marker when the sync
reaches it. Running `offsite` on any deleted session later gives the confirmation for free, instead
of paying for it in waiting. If two complete passes go by with no hide marker anywhere, *that* is
the signal to stop and investigate.

One thing this does **not** do is help the sync. Deleting all 1,120 tars removes a lot of bytes but
only 1,120 objects from a tree holding millions, so it will not measurably reduce the listing work
that appears to be saturating rclone's core. Do not expect the deletions to fix the backlog.

## The eleven pre-delete conditions

Nothing is removed unless all eleven hold. C1-C5 and C7 are metadata only; the rest read or
transfer whole files.

| | condition | why it is there |
|---|---|---|
| **C1** | the run log records this session compressed ok | it was actually done, not assumed |
| **C2** | the `.wfz` exists on the server at the size the log recorded | the replacement is present and whole |
| **C3** | format >= v2 with a `source_tar_sha256`, and the run log and receipt **agree** about it | v1 files cannot prove byte-identity; disagreement means confusion about which archive this is |
| **C4** | the tar is still exactly the size that hash was taken over | if it changed, the hash describes different bytes |
| **C5** | a receipt exists and claims byte-identity | the compressor's own verified claim |
| **C6** | the **local** `.wfz` rebuilds `source_tar_sha256` **today** | proves the replacement still works now, not just when written |
| **C7** | the `.wfz` is in B2 at a matching size | gate condition 7 from `DELETION_PLAN.md` |
| **C8** | the tar on disk re-hashes to `source_tar_sha256` | the bytes about to be deleted really are the ones proven redundant |
| **C9** | B2's `.wfz` is byte-identical to the server's | the sync transferred it correctly, not just something of the right size |
| **C10** | B2's `.wfz` rebuilds `source_tar_sha256` | the **offsite** copy is a working archive on its own |
| **C11** | B2's `widefield.tar` hashes to `source_tar_sha256` | this is precisely the object a restore would pull back |

Two of these deserve comment.

**C6 and C10 are not redundant.** C6 proves the copy on the server works. C10 proves the copy in
Backblaze works. If the server's disk quietly rots, C6 fails and C10 is what saves you; if the
sync corrupted something, the reverse. Checking one and assuming the other is the mistake this
pair exists to prevent.

**C11 is the change from the original plan.** `B2_RESTORE_TEST.md` proposed deleting first and
verifying the B2 tar afterwards. Verifying it *before* means no copy is ever deleted on the
strength of a backup nobody has read back.

## What each one costs

Measured on the 1.19 GB candidate, **while the compression campaign was running** — which matters
enormously, see below.

| | seconds | rate | per GB of tar |
|---|---|---|---|
| C1-C5 | < 1 | metadata | flat |
| C7 | 3 | one B2 API call | flat |
| C6 | 64 | 18.6 MB/s rebuilt | ~54 s |
| C8 | 23 | 53 MB/s over SMB | ~19 s |
| C9 | 138 | B2 download 4 MB/s (anomalous), then hashing at 123 and 55 MB/s | ~20 s at a normal download rate |
| C10 | 41 | 29 MB/s rebuilt, local disk | ~34 s |
| C11 | 31 | B2 download 38 MB/s | ~26 s |
| **total** | **300** | | **~155 s per GB of tar** at normal B2 rates |

**B2 download speed is the wild card:** 4 MB/s in this run, 38 MB/s later in the same run, and
53-96 MB/s in the earlier `b2_restore_test.py` runs. Assume nothing; budget generously.

**Two things make these numbers pessimistic for the campaign proper.** First, a second reader on
the share gets ~20-55 MB/s while compression runs and **645 MB/s when it does not** — so C6 and C8
get roughly ten times faster once the campaign finishes. Second, these are per-archive constants
against a 1.19 GB tar, and the average widefield tar is ~106 GB.

### Which means the full gate does not scale to 1,120 archives

At 155 s/GB, an average 106 GB archive would take **~4.6 hours** of checking. Over the corpus that
is not a plan. Tier it:

| tier | conditions | when | cost |
|---|---|---|---|
| **1 — every archive, always** | C1-C5, C7 | before any deletion | ~3 s each, ~1 h for all 1,120 |
| **2 — every archive** | C6, C8 | before its deletion | ~11 min per 106 GB archive **once the campaign is done**; ~2 h each while it runs |
| **3 — sampled, say 5-10%** | C9, C10, C11 | per batch | pulling all 24 TB back out of B2 to verify it is not sensible |

The first deletion gets all eleven, plus the post-delete offsite proof. That is the point of doing
one first.

## Tiers: what authorises a deletion

The full gate is ~155 s per GB of tar, so on a 161 GB median archive it is 6.5 hours and on the
whole corpus it is not a plan. Two tiers, and **which one authorised each deletion is recorded**:

```powershell
# tier 1 - C1-C5 and C7 only, seconds per archive
python scripts/delete_tar.py --bucket sahalebackup check SESSION --tier cheap
python scripts/delete_tar.py --bucket sahalebackup delete SESSION --confirm SESSION --allow-cheap

# tier 2 - all eleven, hours per archive
python scripts/delete_tar.py --bucket sahalebackup check SESSION
python scripts/delete_tar.py --bucket sahalebackup delete SESSION --confirm SESSION
```

**`--allow-cheap` has to be asked for.** Without it, `delete` accepts only a full check and says
`refusing: no passing full check for ...`. A cheap check is a screening pass and says nothing about
whether the `.wfz` still decodes, so it must not silently authorise removal. Ledger rows written
before tiers existed count as full, so the four archives verified on 08-18/21 keep their standing.
`data/deletions.jsonl` records `check_tier` for every deletion, so anyone can later separate the
tars removed on full evidence from those removed on screening evidence.

### What the cheap tier does and does not establish

C2 and C7 compare **sizes**, not content. A `.wfz` that is the right size but corrupt in both
places would pass. What stands behind that gap:

- every `.wfz` was fully verified at compression time, and the campaign is 533/533 byte-identical;
- ZFS checksums every block and the pool reports zero errors, so silent rot on the server would be
  reported by the storage layer rather than discovered here;
- B2 verifies SHA-1 on upload;
- the 31-day ZFS snapshot window makes any deleted tar locally recoverable for a month;
- and empirically the deep checks have run on four archives - 43 measured conditions plus one
  derived - with no surprises.

**Pace the batches.** The argument is not that a batch is individually risky; it is that deleting
everything in one week means any systemic problem surfaces after the ZFS window has closed on all
of it at once. Deleting gradually means a problem shows up while most tars still exist.

## The procedure

```powershell
# 1. read-only. Writes its verdict to data/deletion_checks.jsonl.
python scripts/delete_tar.py --bucket sahalebackup check test/2026-02-17/1

# 2. the only destructive step. Refuses unless a passing check exists from the last 24 h over a
#    tar of the same size AND mtime, re-runs every cheap condition first, and requires --confirm
#    to repeat the session name exactly.
python scripts/delete_tar.py --bucket sahalebackup delete test/2026-02-17/1 \
    --confirm test/2026-02-17/1

# 3. after the 22:00 Cloud Sync run. Proves B2 hid the object rather than losing it, and that the
#    retained prior version still hashes to source_tar_sha256.
python scripts/delete_tar.py --bucket sahalebackup offsite test/2026-02-17/1
```

Every deletion is recorded in `data/deletions.jsonl` with its evidence, and in
`data/fileEditLog.csv` alongside everything else the project has ever written.

The guards, each with a test: the confirmation must match exactly; a stored check is unusable if
it is stale, failing, for another session, or taken over a tar of a different size or mtime; the
cheap conditions are re-run at the last moment so a regression between check and delete still
stops it; and `os.remove` appears exactly once in the module, pointed at the tar.

---

## Two ZFS findings that change when the space actually comes back

Neither blocks the first deletion. Both matter for the campaign.

### 1. Space returns after ~31 days, not immediately

`data/data` takes **daily automatic snapshots and retains 31 of them** (oldest
`auto-2026-07-18`, newest `auto-2026-08-17`). A deleted tar's blocks stay allocated to those
snapshots until they age out. So:

- deleting a tar frees pool space after **~31 days**, and
- the B2 billing clears after **30 days** (`daysFromHidingToDeleting`).

Those line up almost exactly, which is convenient: one month after a deletion batch, both the
pool space and the Backblaze bill reflect it. Do not expect `zfs list` to move before then.

This is also a **third recovery path** nobody has been counting, and it is now **verified**: for 31
days after a deletion the tar is readable straight out of
`/mnt/data/data/.zfs/snapshot/auto-YYYY-MM-DD_00-00/Subjects/...`, locally, with no B2 involvement.
Confirmed by hashing the deleted `test/2026-02-17/1` tar out of `auto-2026-08-18_00-00` and getting
`source_tar_sha256` back in 6.5 s. `snapdir` is `hidden`, so `.zfs` works as a path but will not
appear in `ls`, and **the directory name includes the `_00-00` time suffix**.

### 2. Two snapshots from 2024 never expire, and they pin 12 TB of the deletable set

```
data/data@manual-2024-02-01_13-26        used 0B    refer 101T   Thu Feb  1 13:27 2024
data/data@bigdata-snap-2-1-24--4-09pm    used 0B    refer 101T   Thu Feb  1 16:09 2024
data/data@manualtest1-2023-10-13_16-55   used 332K  refer 1.29M  Fri Oct 13 16:55 2023
```

They show `used 0B` only because everything they reference is still live. **Delete a file that
existed on 2024-02-01 and its blocks do not free — they become uniquely held by those snapshots,
which have no expiry.**

Stat'ing the tars of all 212 currently-`SAFE` sessions by modification time:

| | sessions | tar bytes |
|---|---|---|
| written after 2024-02-01 — only the 31-day dailies hold them | 152 | **29.13 TB** |
| written before — **pinned indefinitely** by the 2024 snapshots | 60 | **11.98 TB** |
| total | 212 | 41.11 TB |

So of the 41 TB deletable today, **~12 TB would free no space at all** until someone destroys those
two 2024 snapshots. Extrapolated across the full 119.4 TB corpus the pinned fraction will be
larger, since more of the older sessions predate February 2024.

**This is not a reason to stop.** Deleting a pinned tar still removes it from the live tree, still
propagates to B2, and still stops the *Backblaze* bill after 30 days. It just does not return
local pool space. But it does mean:

- **the "reclaim 119 TB on sahale" figure is optimistic**, and how optimistic depends on those two
  snapshots;
- somebody should find out **why they exist and whether they are still needed**. They are from
  February 2024, named `manual-` and `bigdata-snap-`, and appear to be a one-off from some
  migration. Destroying them is a decision for whoever made them, not for this project;
- the candidate for the first deletion is **not** pinned, so it is a clean test of the mechanism.

Worth confirming with the server admin before the bulk campaign, since it changes the headline
number the whole project is justified by.
