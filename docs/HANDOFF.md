# Handoff: everything needed to pick this up cold

Written 2026-08-13, updated 2026-08-17. Read this first, then `README.md`. Every other doc
referenced here is in `docs/`.

> ## The deletion campaign is COMPLETE too, 2026-08-28
>
> ```
> tars deleted        1,112        all verified gone
> bytes freed         118.68 TB
> replaced by          48.36 TB of .wfz across 1,117 archives
> authorised by       4 full-gate checks, 1,108 cheap-tier
> ```
>
> **Only five tars remain**, and they are exactly the five partials - `AL_0039/2025-10-01/1`,
> `AL_0045/2026-02-05/1`, `AL_0045/2026-02-09/1`, `AL_0046/2026-03-02/1`, `ZYE_0098/2026-01-02/1` -
> refused at condition C3 because `source_tar_sha256` is null by design. Deleting those is a
> separate decision. Plus the 2 truncated archives, which were never compressed, and 6 zero-byte
> files: 13 `widefield.tar` left on the share in total.
>
> The batch took 61.7 minutes for 1,068 archives. **One transient failure**,
> `AL_0035/2025-01-25/1`: the cheap conditions are re-run immediately before removal and C7 briefly
> could not reach B2, so `delete` refused with "something changed since the check" and left the tar
> alone. Re-checked minutes later, everything passed and it was deleted. **That is the guard working
> exactly as intended** - it declined to act on evidence it could not re-confirm.
>
> ### The space has not come back yet, and that is expected
>
> ```
> usedbydataset    280T -> 182T      the tars are out of the live tree
> usedbysnapshots  4.76T -> 109T     ...and now held by the 31-day ZFS snapshots
> used             291T (unchanged)
> ```
>
> Pool space returns as those snapshots age out, around **2026-09-28**, except for whatever predates
> the two never-expiring Feb-2024 snapshots. B2 storage clears 30 days after each hide marker
> appears. Do not read the unchanged `used` figure as a failure.
>
> ## The widefield campaign is COMPLETE, 2026-08-27
>
> ```
>                       archives      source   compressed    ratio
> faithful                 1,112   118.68 TB     48.16 TB   x2.464
> partial (see below)          5     0.55 TB      0.20 TB   x2.754
> TOTAL                    1,117   119.23 TB     48.36 TB   x2.465
>
> verified rebuild      1,117 / 1,117            no verification failure, ever
> reclaimable              70.86 TB  =  $493/month  =  $5,910/year at $6.95/TB/month
>   deletable by the gate  70.52 TB
>   needs a decision        0.35 TB   (the 5 partials)
> ratio range            x2.07 (AB_0003/2021-04-19/1) to x3.80 (Subjects/test/2025-11-04)
> bit-shift              4 on 1,017 archives, 0 on 100
> ```
>
> **The 5 partials were compressed 2026-08-27** after their stray `p0.missed_samples.imec0.txt` was
> proven to exist outside each archive - three already did, two had to be lifted out of the tar
> first with `scripts/extract_stray_member.py`. Ratios x2.61 to x2.99, all five rebuilding their
> frames correctly. **The deletion gate refuses them and should**: checked on
> `ZYE_0098/2026-01-02/1`, condition C3 fails with `sha=False` because `source_tar_sha256` is null
> by design for a partial. Deleting one of those five tars is a separate, recorded decision.
>
> Only **2 archives remain uncompressed**, and both are truncated source data - see below.
>
> ### Re-census 2026-08-27: nothing new, and nothing was missed
>
> Fresh walk of every `*.tar` under `Subjects` (2,556 files):
>
> ```
>  1080   112.30 TB   widefield.tar / <expNum>.tar
>  1474     0.28 TB   _kilosort_raw.output.tar     (190 MB each, not an opportunity)
>     1     0.00 TB   old_widefield.tar            AL_0032/2024-06-12/1, 4.24 GB
>     1     0.00 TB   ._widefield.tar              macOS resource fork, 0 bytes
> ```
>
> - **Zero new widefield tars since the Aug-3 census.** Nothing has been added to compress.
> - **All 44 tars we deleted are confirmed absent** from the server.
> - Six census entries never reached the run log, and **all six are zero-byte files** -
>   `AB_0032/2024-06-12/1`, `AB_0032/2024-08-12/5`, `AL_0034/2024-08-12/5`,
>   `AL_0035/2024-11-24/1`, `ZYE_0089/2024-07-10/5`, plus the `._widefield.tar`. Nothing real was
>   skipped.
>
> ### The 21 "mixed archives" are not refusals - they all compressed
>
> Worth correcting, because the earlier docs leave the impression they are outstanding. All 21 were
> trimmed by the lab from **3.36 TB to 2.13 TB**, which removed the 82 GB ephys members and made the
> archives uniform. The campaign then **compressed all 21 successfully.** See
> `MIXED_ARCHIVES.md`, whose framing predates the trim.
>
> **Seven archives never compressed and never will without a decision:**
>
> | reason | sessions |
> |---|---|
> | `ValueError: buffer is smaller than requested size` | `AB_0032/2024-04-05/1`, `AL_0033/2025-01-09/2` |
> | `UnsupportedArchive`, a small stray member tar'd in beside the frames | `AL_0045/2026-02-09/1` (8,873 B), `AL_0045/2026-02-05/1` (24 B), `AL_0046/2026-03-02/1` (22 B), `ZYE_0098/2026-01-02/1` (57 B), `AL_0039/2025-10-01/1` (1,779 B) |
>
> **Identified 2026-08-27.** All five `UnsupportedArchive` cases have the *same* stray, and it is
> the **last** member of the tar, not the first: `1/p0.missed_samples.imec0.txt`, a SpikeGLX
> dropped-sample log. Every one of the five tars ends cleanly with two zero blocks, so they are not
> truncated - they simply contain one extra small text file. This is the A1 finding from
> `PIPELINE_REVIEW.md`: the tar sweeps the whole session directory.
>
> Whether the stray can be dropped splits the five in two:
>
> | session | stray | same file outside the tar? | tar |
> |---|---|---|---|
> | `AL_0045/2026-02-09/1` | 8,873 B | **yes**, `p0_g0_t0.imec0/`, size matches | 60.36 GB |
> | `AL_0045/2026-02-05/1` | 24 B | **yes**, size matches | 91.95 GB |
> | `AL_0046/2026-03-02/1` | 22 B | **yes**, size matches | 92.71 GB |
> | `ZYE_0098/2026-01-02/1` | 57 B | **no - the tar holds the only copy** | 96.34 GB |
> | `AL_0039/2025-10-01/1` | 1,779 B | **no - the tar holds the only copy** | 206.14 GB |
>
> So the first three are ready for `codec.compress(drop_members=...)` as soon as someone confirms
> the SHA-256 matches (the codec refuses to drop without per-member hash evidence, by design).
> For the last two, extract the text file to the session directory first, then drop and compress -
> that preserves the log rather than discarding it.
>
> Total opportunity: **547.5 GB of tar, roughly 325 GB reclaimable** at the campaign's x2.46. Small
> against 70.5 TB, but it closes the corpus out properly.
>
> ### The two `ValueError` cases are truncated tars, and that is pre-existing data damage
>
> Solved 2026-08-27. **Both archives are truncated: the final member's payload runs past the end of
> the file, and neither has the two zero blocks that mark end-of-archive.**
>
> | session | tar | last member | short by |
> |---|---|---|---|
> | `AB_0032/2024-04-05/1` | 12,751,827,968 B | frame 117252 of 631,826 B | **370,176 B (58% of the frame)** |
> | `AL_0033/2025-01-09/2` | 23,449,961,984 B | `frame-133618` of 627,200 B | **102,912 B (16%)** |
>
> Both file sizes are exact multiples of 512, so the write stopped on a block boundary - a killed
> process, a full disk or a dropped connection, not corruption. `ValueError: buffer is smaller than
> requested size` is the compressor correctly refusing to encode a frame whose bytes are not all
> there. **The tool is right; the data is incomplete.**
>
> Neither session has the frames anywhere else. `AB_0032/2024-04-05/1` holds 44 files and 17 GB with
> no subdirectories and no SVD outputs, so the loose frames were removed after tarring.
> **`AL_0033/2025-01-09/2` contains exactly one file - the truncated tar - and 22 GB, with no session
> metadata at all**, which looks like an aborted session. So the tars hold the only copy of their
> frames, minus the incomplete last one.
>
> **Left alone deliberately.** They could be compressed as partials that drop the incomplete final
> frame, which is unrecoverable in any case, but `codec.compress(drop_members=)` requires a verified
> copy outside the archive and there cannot be one. **Do not fabricate that evidence** - it exists to
> stop exactly this kind of shortcut. A principled option, if the 36 GB is ever worth it, is an
> explicit `allow_truncated_tail` path that drops only a final member whose payload is
> demonstrably short by file-size arithmetic and records it distinctly in the receipt. That is a
> change to a safety mechanism for ~14.7 GB of savings, so it needs asking first.
>
> Worth flagging on its own terms: **two archives on the share have been silently truncated since
> they were written**, one of them an orphan. That is a data-integrity finding independent of
> compression.
>
> **The supervisor was stopped on 2026-08-27** with `D:\temp\wfc_stop` after it had relaunched **22
> times**, each time retrying those same 7 and failing. That loop was not just wasted work: every
> launch created `.wfz.partial-*` files, which is precisely what makes each rclone run exit with
> errors and skip its delete phase. **Stopping it is what should finally let the deletions
> propagate.** Delete the stop file before any future restart.
>
> ## The operative plan, agreed 2026-08-22
>
> 1. **Let the widefield campaign finish** — ~4 days from 08-22, so around Wednesday 08-26.
> 2. **Then delete the remaining tars in one pass**, cheap tier per archive
>    (`delete_tar.py sweep` to screen, then `delete --allow-cheap`).
> 3. **Wait for a clean sync run to propagate those deletions** and confirm a hide marker with
>    `delete_tar.py offsite <session>`.
> 4. **Only then start the ephys campaign.**
>
> Step 4 is the point Nick raised and it is correct. `scripts/ephys_compress.py` writes
> `*.cbin.partial-<pid>` and `*.ch.partial-<pid>` **inside `/mnt/data/data/Subjects`**, the synced
> tree, and renames them on success. That is the same churn that makes every rclone run exit with
> errors and therefore skip its delete phase. Starting ephys before the deletions have propagated
> would re-block them indefinitely.
>
> **Timing wrinkle:** runs chain nearly back-to-back — run 69940 failed at 23:20:46 and the next
> started at 23:20:49, three seconds later — and each takes ~73 h to check ~6 M objects. So there is
> no quiet window in which to delete. Deleting during a run guarantees *that* run also errors, but
> nothing is lost: the following run, with no temp-file churn, propagates everything at once. Expect
> the hide markers roughly **3-4 days after the deletions**, then B2 billing clears 30 days later.
>
> **The 31-day ZFS snapshot margin is what makes one big pass safe.** It is far longer than the 3-4
> days needed to confirm propagation, so if anything were wrong every tar is still recoverable
> locally for about four more weeks.
>
> **Still worth the Monday email:** adding `*.partial-*` to the Subjects task exclude list decouples
> all of this. Runs would stop erroring immediately, deletions would propagate at the end of
> whichever run is in progress, and **ephys could then run without blocking anything**. It saves
> roughly a week of waiting and roughly $470/month of tars sitting in B2 that we have already
> deleted locally.
>
> ## What changed since it was written (2026-08-17)
>
> - **Widefield: 326 of 1,120 archives**, 58.18 TB -> 24.20 TB, x2.40, **326/326 byte-identical**.
>   794 archives / 61.60 TB remain.
> - **The offsite backup has fully caught up: 326/326 `.wfz` in B2, 100%, backlog zero.**
>   §6 condition 7 now passes on everything compressed. The backup is no longer the constraint;
>   see the banner in `B2_THROUGHPUT.md` for the caveat on that claim.
> - **The campaign was stopped for 76% of 08-13 to 08-17** — the sahale outage, then a second
>   silent death on 08-14 21:07 that nobody noticed for 2.8 days. Restarted 08-17 17:04.
>   Full timeline and the unapplied Scheduled Task fix: `DOWNTIME_AND_PERSISTENCE.md`.
> - **Ephys ran for 34 minutes on 08-13, completed zero files, and took the file server down.**
>   Do not restart it while widefield is running. It left 121.4 GB of stale `.cbin.partial-*` on
>   sahale, listed in `DOWNTIME_AND_PERSISTENCE.md`.
> - **Step 0 of the deletion gate is largely done and it passed.** The B2 copy was downloaded and
>   proven to rebuild the original tars byte-for-byte — 3 sessions, both code paths, 9/9 checks.
>   Reusable: `scripts/b2_restore_test.py`. No deletion was needed or performed.
> - **The B2 undo window is 30 days, not the 60 the docs assumed** (`daysFromHidingToDeleting: 30`,
>   read from the API). Halves the review budget and the retention cost. See `B2_RESTORE_TEST.md`.

---

## 1. What this project is

The Steinmetz lab's file server (**sahale**, `Y:` on this workstation) holds ~258 TB. Two large
categories are stored uncompressed and are pure waste:

| | size | plan | status |
|---|---|---|---|
| widefield `widefield.tar` | 119.4 TB across 1,120 archives | JPEG-LS → `.wfz`, ×2.40 | **campaign running, 29% done** |
| raw SpikeGLX `*.bin` | 95.0 TB across 1,938 files | mtscomp → `.cbin`, ×2.56 | **started 08-13, overloaded sahale, stopped** |

Together ~128 TB reclaimable, worth **~$10,700/year** of Backblaze at the confirmed $6.95/TB/month.
See `SPACE_AND_COST.md`.

**Nothing has ever been deleted.** `wfcompress` has no delete path anywhere, by design, and neither
does the ephys driver. Deletion is a separate gated decision — see §6.

---

## 2. Where things live

| | |
|---|---|
| repo | `D:\Dropbox\code\widefieldCompress` (GitHub `SteinmetzLab/widefieldCompress`) |
| **venv — use this, not system python** | `D:\temp\wfc-venv\Scripts\python.exe` |
| the share | `Y:` = `\\sahale.biostr.washington.edu\data`; on sahale itself `/mnt/data/data` |
| staged for sahale | `Y:\temp\pylibs\` — `mtscomp.py`, `tqdm/`, `ephys_compress.py`, two benchmarks |
| B2 CLI | `D:\temp\wfc-venv\Scripts\b2.exe`, already authorized **read-only**, bucket `sahalebackup` |
| SSH key for sahale | `C:\Users\nicks\.ssh\sahale_wfc` — **installed and working** |

System `python` is **not** the project interpreter and lacks `imagecodecs`/`tifffile`. Always use
the venv. Every PowerShell call needs `-NoProfile -NonInteractive` per the user's global rules.

---

## 2a. First thing to run

```powershell
D:\temp\wfc-venv\Scripts\python.exe status.py
```

One command, read-only, reports both campaigns and the backup: how many archives and files are
done, whether the processes are alive, when the last completion was, and the latest B2 snapshot.
`--skip-ssh` if sahale is unreachable. Everything below is detail behind that.

**Both campaigns survive this session ending.** The ephys job on sahale is parented to `init`
(PID 1) — fully daemonised by `nohup`, and it has already survived several SSH sessions closing.
The widefield supervisor on Windows has been orphaned from the shell that launched it. Neither is
a child of any agent process. The one residual uncertainty is whether Windows tears down a job
object on Claude Code exit; if the widefield job is ever found stopped for no reason, that is the
first suspect, and the fix is to register the supervisor as a Scheduled Task (not done — it is
persistent system configuration and needs the user's say-so).

## 3. What is running right now

**The widefield campaign, under a supervisor.**

```
pythonw scripts\supervise_bulk.py          # relaunches the driver when it exits
  └── pythonw -m wfcompress.lab.batch --census data/census_Y.csv --server Y
        --jobs 8 --threads 4 --largest-first --assume-shape 560 560
        --log data/bulk.jsonl --file-log data/fileEditLog.csv
```

Check it:

```powershell
Get-Content D:\temp\wfc_supervisor.log -Tail 5
Get-Content D:\temp\wfc_run_001.out -Tail 5
D:\temp\wfc-venv\Scripts\python.exe scratch.py     # progress summary
```

Stop it cleanly: `New-Item D:\temp\wfc_stop` (lets the current run finish). Delete that file
before restarting.

**Progress at handoff: 302 archives, 54.77 TB → 22.78 TB, ×2.40, 302/302 byte-identical.** 818 of
1,120 remain, roughly two weeks at the observed ~67 MB/s.

### The driver keeps dying — and we finally know why

It stopped unexplained three times, then on 2026-08-13 exited **3221225786 = 0xC000013A =
STATUS_CONTROL_C_EXIT** after 35 h, because the user closed a console window it had popped up. The
supervisor was launching `python.exe`, which allocates a console; closing it sends a control event
that kills the process. Now fixed — the supervisor spawns `pythonw.exe`, which has no console. If
it dies again with a clean empty stderr, suspect the same class of thing.

The driver is safe to kill at any point: it resumes from `data/bulk.jsonl`, re-checks that each
recorded `.wfz` still exists at the recorded size, and reclaims stale `*.partial-*` files
automatically (it has recovered 297, 184 and 163 GB doing so).

---

## 4. The widefield side, in brief

`.wfz` = magic + uint64 footer offset + concatenated JPEG-LS codestreams + a zip footer holding
`meta.json`, `index.npy`, `order.npy`, the original tar headers, the TIFF "shells" and the trailer.
Rebuilds the source tar **byte-for-byte**; `wfcompress check` proves it by streaming the
reconstruction through SHA-256 without writing anything.

Things that were learned painfully and should not be re-derived:

- **Bit-shift normalisation is worth ×1.63 → ×2.76.** Cameras write 9–12 bit samples left-shifted
  into 16-bit words; stripping the always-zero low bits is most of the ratio.
- **Processes beat threads ~2×** (`--jobs` over `--threads`); numpy around the codec holds the GIL.
- **Write buffering matters enormously over SMB.** 256 kB writes get 14 MB/s, 16 MB writes get
  131 MB/s. `codec.WRITE_BUFFER` is 16 MB for this reason.
- **`os.scandir`, never `Path.iterdir()` + `.is_dir()`** for walks — the latter is a stat per entry
  and took a 576-entry listing from 5 s to 283 s over SMB.
- Some archives are **big-endian TIFF** (`>u2`), others little-endian raw. `extract --bin`
  normalises to little-endian; the README beside each `.wfz` quotes the dtype it actually writes.

`wfcompress extract` gets data out without materialising a tar: `extract X.wfz dir/` for the
original frame files, `extract X.wfz out.bin --bin` for a flat `rows×cols×nFrames` uint16 array in
acquisition order.

---

## 5. The ephys side — built, not started

`scripts/ephys_compress.py`, staged at `/mnt/data/data/temp/pylibs/`. **Runs on sahale**, not here:

```bash
python3.9 /mnt/data/data/temp/pylibs/ephys_compress.py --root /mnt/data/data/Subjects --dry-run
python3.9 /mnt/data/data/temp/pylibs/ephys_compress.py --root /mnt/data/data/Subjects --procs 8 --threads 4
```

Why there and not here, all measured:

| | |
|---|---|
| sahale | 2× Xeon Silver 4210R, **40 threads**, 273 GB RAM, numpy 1.22.4 already installed |
| local pool read | **423 MB/s** |
| second reader over SMB from the workstation, campaign running | **~20 MB/s** |
| mtscomp, 8 processes × 4 threads, on sahale | **139 MB/s → 7.9 days** for the whole corpus |
| mtscomp, 1 process × 32 threads | 47 MB/s — its verify pass is serial and only overlaps across processes |

There is no pip on sahale, which is why `mtscomp.py` and `tqdm` are staged as plain files. It needs
**no compiler and no root**. `wfcompress` itself can never run there — `imagecodecs` has no FreeBSD
wheels.

Reach the box like this — `BatchMode=yes` matters, so it fails rather than ever prompting for a
password:

```powershell
ssh -i "$env:USERPROFILE\.ssh\sahale_wfc" -o BatchMode=yes 'NETID\nsteinme@sahale.biostr.washington.edu' 'uptime'
```

Verified 2026-08-13. The pool is `data/data`, 373 T with 111 T free (70% used), load average ~1.8.

A process sweep at 8/12/16/20 was requested but the result has not come back; 8 is known-good.

**Dry-run on the box, 2026-08-13 — the driver works:**

```
1939 raw .bin found in 1630 s; 0 unreadable directories
39 to skip:
      26  no .meta, so channel count and sample rate are unknown
      12  already has a .cbin and .ch
       1  size 9619200000 is not a whole number of 385-channel int16 samples
1900 to compress, 94.64 TB
expect roughly 7.9 days at 139 MB/s
```

Two things to note. The **discovery walk takes 27 minutes even locally** — the same pathological
session directories that made the SMB census slow — so it is not a hang. And the refuse-rather-
than-guess rule earned its keep immediately: **one file is not a whole number of samples**
(9,619,200,000 bytes at 385 channels), meaning it is truncated or its `.meta` is wrong. Someone
should look at it before it is compressed.

I also checked whether derived copies inside sorter-output directories were inflating the corpus.
They are not: **3 files, 0.35 TB**, of which one is a real duplicate — a 347.5 GB
`whole_train_artifact_removed_full` copy under `JRS_0057/2026-06-04/1/kilosort4/`. Not worth
special handling.

---

## 6. Deletion — the gate, and what blocks it

`DELETION_PLAN.md` is the full proposal. Seven conditions, all evaluated at deletion time:

1. run log records success; 2. `.wfz` exists at the recorded size; 3. format ≥ v2 with a
`source_tar_sha256`, not partial; 4. **the tar is still the size that hash was taken over**;
5. a receipt claiming byte-identity; 6. **the `.wfz` reproduces that hash today**; 7. **the `.wfz`
is present in Backblaze**.

Run the audit with `scripts/audit_deletable.py` (`--strict --rehash-tar` for 6 and 8).
**Conditions 1–6 pass on everything tested. As of 2026-08-17 condition 7 passes too — all 326
`.wfz` are in B2. The paragraph below is the 08-13 state, kept because it shows how fast this
reading has changed twice.**

> **2026-08-13: 182 of 302 `.wfz` are in B2 (14.91 TB); 120 (7.87 TB) are not — 65% offsite, up
> from 46% two days earlier.** The original tars are all still there, so nothing is at risk; what
> is missing for those 120 is the *replacement*, and deleting their tars would leave a single copy.

**The backlog is now draining, and this reverses an earlier conclusion.** On 11 August B2 was
taking ~1.4 TB/day against ~2.4 TB/day of production, so I said the backup gated deletion. Since
then it has moved 7.02 TB in two days — **~3.5 TB/day, faster than we produce** — and the backlog
fell from 9.20 to 7.87 TB despite 82 new files being written:

```
2026-08-11   50 files   4,318 GB
2026-08-12   40 files   3,199 GB
2026-08-13   33 files   2,216 GB   (partial day)
```

It is also demonstrably back-filling older files, not just taking new ones. Whether the admin
changed rclone `--transfers` or it simply caught up after the 8–9 August stall is unknown; worth
asking. At this rate the ~34.8 TB still to upload finishes roughly in step with the campaign, so
**B2 is no longer the binding constraint** — but keep watching it, because the earlier reading was
wrong once already.

`scripts/snapshot_b2.py --bucket sahalebackup` takes a dated snapshot into `data/b2_snapshots/`
and diffs against the previous one. Run it every couple of days.

**Step 0 has not been done**: delete one small tar, restore it from B2, confirm the restored bytes
hash to `source_tar_sha256`. I recommend inverting it — confirm and hash-check the B2 copy *first*,
then delete — so no verified copy ever depends on an untested restore. See `B2_RESTORE_TEST.md`.

---

## 7. Loose ends

- **A new mixed archive appeared**: `AL_0039/2025-10-01/1`, members of 1,779 and 631,826 bytes —
  a different shape from the 21 known ones (those had 82 GB ephys files). Refused safely; not
  investigated.
- **The 21 original mixed archives were trimmed by the lab**, 3.36 → 2.13 TB. Verified: not us
  (no `.tar` writes in `fileEditLog.csv`), and all 21 ephys recordings still exist outside at
  matching sizes. `codec.compress(drop_members=...)` exists for future ones and refuses to drop
  anything without per-member SHA-256 evidence.
- **`PIPELINE_REVIEW.md`** — 8 serious findings in `Dropbox/code/Pipelines/widefield`, unread by
  the user so far. A1 (the tar sweeps the whole session directory) is why mixed archives keep
  appearing. A4 (frame timestamps assume strict blue/violet alternation while the SVD does not) is
  the one with scientific consequences. **No edits were made to that folder.**
- **`.phy` folders**: 96 folders, 25.0 GB, listed in `data/phy_folders_Y.csv`; the lab was going to
  decide.
- 26 ephys files (224 GB) have **no `.meta`** and will be skipped by the driver.

---

## 8. Working style the user has asked for

- **Only the final message of a turn is read.** Start it with `# This is the final message of my
  turn`, re-orient from scratch, repeat what matters, say what is unfinished. Assume they are
  returning after days.
- American English. Arial, despined axes, units on every axis for any figure.
- **Use `scratch.py`, never `python -c`** — the latter triggers an approval prompt every time.
- Data ≤100 MB in the cwd, 100 MB–10 GB under `D:/temp/`, >10 GB ask first.
- Long explanations go in a `.md`, not the chat.
- The user is a PI juggling many projects; they will not remember prior context. Say the number,
  not "as established earlier".
