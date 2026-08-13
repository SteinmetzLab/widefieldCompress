# Handoff: everything needed to pick this up cold

Written 2026-08-13. Read this first, then `README.md`. Every other doc referenced here is in
`docs/`.

---

## 1. What this project is

The Steinmetz lab's file server (**sahale**, `Y:` on this workstation) holds ~258 TB. Two large
categories are stored uncompressed and are pure waste:

| | size | plan | status |
|---|---|---|---|
| widefield `widefield.tar` | 119.4 TB across 1,120 archives | JPEG-LS → `.wfz`, ×2.40 | **campaign running, 27% done** |
| raw SpikeGLX `*.bin` | 95.0 TB across 1,938 files | mtscomp → `.cbin`, ×2.56 | **driver built, not started** |

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
**Conditions 1–6 pass on everything tested.** Condition 7 does not:

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
