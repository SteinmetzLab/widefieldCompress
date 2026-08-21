# Why the B2 backup is falling behind, and what could be done about it

> ## Resolved 2026-08-17: the backlog is gone
>
> **326 of 326 `.wfz` are offsite, 24.20 TB, 100%.** The 7.87 TB backlog measured on 08-14 is
> zero. Condition 7 of the deletion gate now passes on everything compressed so far.
>
> B2's own upload timestamps for the days since:
>
> ```
> 2026-08-14   20 files   1,264 GB
> 2026-08-15   57 files   3,927 GB
> 2026-08-16   67 files   4,094 GB
> ```
>
> **Read the caveat before concluding the problem is solved.** Those two 4 TB days had no
> competing load: the widefield campaign was dead from 08-14 21:07 to 08-17 17:04, so sahale's
> pool was serving the sync alone. The honest reading is that the sync can do ~4 TB/day *when the
> pool is otherwise idle*, against ~2.75 TB/day of production. With the campaign running it did
> 4.32, 3.20 and 2.22 TB/day on 08-11 to 08-13 — bracketing production rather than clearly beating
> it. Re-snapshot in a few days now that the campaign is running again; that is the measurement
> that settles it.
>
> ### That measurement, taken 2026-08-19: with the campaign running, B2 falls behind by ~1 TB/day
>
> ```
> 386 of 409 .wfz offsite, 27.44 TB in / 1.19 TB out (96%)   23 files pending
> 2026-08-17   0 files       0 GB   (campaign down, backlog already empty - nothing to send)
> 2026-08-18  30 files   1,658 GB   (campaign running again from 00:04 UTC)
> 2026-08-19  30 files   1,588 GB   (partial day, to 15:47 UTC)
> ```
>
> Against ~2.66 TB/day of `.wfz` production, so the backlog grows ~1 TB/day. Compare the two
> campaign-idle days, 3.93 and 4.09 TB/day: **pool contention with the compression campaign costs
> the sync roughly 2.5x.** That is the same effect measured in `RUNNING_BOTH_CAMPAIGNS.md` from the
> other direction.
>
> Projection, with ~10 days of compression left: the backlog peaks near **10-11 TB**, then drains at
> ~4 TB/day once compression stops, so a complete offsite copy roughly **13 days out**. Far better
> than the 35 TB peak feared on 08-14, and small enough that it does not gate the deletions of
> already-uploaded archives. Keep sampling it.
>
> Nothing in the analysis below was acted on. All five recommendations are still open, and items 1
> and 3 (the lifecycle rule for unfinished large files, and excluding `*.partial-*`) are still
> worth doing on cost grounds alone regardless of throughput.

Measured 2026-08-14. Everything here is read-only observation: the Cloud Sync config was read
from the TrueNAS database in SQLite read-only mode, the bucket via the read-only application key,
and the link counters via `netstat`. **Nothing was changed.**

> ## 2026-08-20: the bottleneck looks like rclone's own single-core CPU
>
> Caught the sync in the act. `pgrep -lf rclone` on sahale:
>
> ```
> 69940 /usr/local/bin/rclone --config /tmp/tmpwd5hjhoz -v --stats 1s --fast-list \
>       --transfers 20 --exclude .zfs --exclude .zfs/** \
>       sync /mnt/data/data/Subjects remote:sahalebackup/subjects
>
> PID   STAT %CPU %MEM     RSS      ELAPSED       TIME
> 69940 R    88.9  1.5 3941016   1-22:02:16   2671:17.32
> ```
>
> **One process, 46 hours elapsed, 44.5 hours of CPU — about 97% of a single core, sustained, for
> nearly two days.** Load average on the 40-thread box is 1.93. Thirty-nine threads are idle while
> the backup is pinned to one.
>
> Three consequences, in order of how much they change the picture:
>
> 1. **The daily schedule is irrelevant and "run it more often" is dead as a lever.** This is the
>    *Monday* 22:00 run. Tuesday's and Wednesday's never started, because TrueNAS will not launch a
>    second run while one is going. Duty cycle is effectively 100% — the opposite of what the
>    retracted analysis below concluded. There is no idle time to reclaim.
> 2. **It is CPU-bound, not I/O-bound or bandwidth-bound.** 97% of a core for 46 h is not the
>    profile of a process waiting on a network or a disk. Total interface output measured over
>    ~4 minutes was ~31 MB/s including all the SMB traffic serving the compression campaign, i.e.
>    2.5% of the 10 Gb link.
> 3. **`--fast-list` is on.** For a bucket holding ~200 TB it makes rclone build and hold the entire
>    remote object listing in memory before and during the comparison — the 3.9 GB RSS. On very
>    large remotes this is a known way to make rclone slow to start and expensive to run, and it is
>    the most likely thing eating that core.
>
> **What it is actually computing, I cannot see.** TrueNAS gives rclone `-v --stats 1s` but the
> output is not in `/var/log`, and `procstat -f` on a root-owned process is not readable as this
> user. So "CPU-bound" is a solid inference from `ps`; *why* is not established.
>
> **Symptoms consistent with it:** no large-file upload has completed in roughly 15 hours (the
> newest finished object *started* at 08-19 10:17 UTC), ten uploads have been open 11+ hours, and
> the backlog doubled in 11 hours — from 23 files / 1.19 TB at 08-19 15:47 to **47 files / 2.37 TB**
> at 08-20 02:46, with the count of objects offsite unchanged at 386. That is ~2.6 TB/day of
> accumulation, well above the 0.7 TB/day estimated a day earlier.
>
> **The lever this suggests, and it is a different one from anything above:** split the single
> `Subjects` task into several Cloud Sync tasks by prefix (`AB_*`/`AL_*`, `FD_*`/`JRS_*`, `SM_*`,
> `ZYE_*`, everything else). Each task is its own rclone process, so they run on separate cores,
> each walks a smaller tree, and each holds a smaller listing. That attacks a single-core limit in
> the only way available without upgrading rclone — this is v1.57.0-DEV from late 2021, and
> upgrading a production appliance for this is not advisable. Also worth trying: **dropping
> `--fast-list`**, which is a one-field change to the task's `args`.
>
> **Do not split it into twenty.** `RUNNING_BOTH_CAMPAIGNS.md` measured what happens when too many
> concurrent readers hit this pool: a second sequential reader dropped from 645 to ~20 MB/s, and
> eight compression workers plus eight ephys workers took the server off the network entirely.
> Three or four tasks, watched.
>
> ## Retracted 2026-08-19: the duty-cycle finding below is not supported by its evidence
>
> **B2's `uploadTimestamp` on a large file is the moment of `b2_start_large_file`, not completion.**
> Proven directly: `ZYE_0066/2022-10-25/6/widefield.wfz` (58.15 GB) and
> `ZYE_0052/2021-12-18/2/widefield.wfz` (62.88 GB) were both still listed as *unfinished* at
> 2026-08-18 16:28 UTC, and both carry `uploadTimestamp` 15:43:47 and 15:43:50 — 45 minutes
> earlier, exactly the start stamps embedded in their file ids.
>
> Everything in "**1. Duty cycle**" below was derived by treating those stamps as completion times
> and reading the gaps between them as idleness. That inference does not hold. With
> `--transfers 20`, rclone opens twenty transfers within seconds and then starts no more until
> slots free, so **starts cluster and leave long gaps precisely while transfers are running hardest.**
> Re-running the same method today produced apparent rates of 5,510 and 104,452 MB/s, which is how
> the flaw announced itself.
>
> **So: the "49% duty cycle", the "43 MB/s aggregate", and the headline claim that the sync
> "spends about half its life not running" are all withdrawn.** Not disproven — unmeasured. And
> recommendation 4 below, "run the task more often", loses its stated justification: it was called
> the single biggest lever on the strength of that 49%.
>
> **What replaces it.** The reliable instrument is `b2 file large unfinished list b2://sahalebackup`,
> which shows genuinely in-flight transfers in real time. Two samples: 16 in flight at 08-18 16:28
> UTC, 9 in flight at 08-19 15:46 UTC — both mid-afternoon, far from the 22:00 start, so runs are
> long rather than brief. At 08-19 15:46, 10.8 h into that day's run, the lexical walk had only
> reached `subjects/AL_00xx`; if a full pass over the ~200 TB tree takes more than 24 h then
> TrueNAS will not start another run anyway and scheduling is not the lever at all. Measuring this
> properly means polling that listing on a timer, or reading the task log in the TrueNAS UI.
>
> **What is still sound below:** the table of things ruled out (NIC, CPU, chunk size, `--transfers`,
> bwlimit), the 128 MB/s sustained WAN measurement, the FIFO-not-starvation finding in §2, and the
> unfinished-large-file billing leak in §3. Per-day byte totals also remain usable as a trend, since
> they attribute a transfer to the day it *started* and almost all finish the same day.

## The short version

The pipe is not the problem. The sync **spends about half its life not running**, and when it is
running it averages about a third of a rate it has already demonstrated it can hit. Both of those
point at scheduling and rclone behavior, not at bandwidth.

## What is not the bottleneck

| Suspect | Measurement | Verdict |
|---|---|---|
| NIC | `bnxt1`, **10Gbase-SR full duplex, up** | not the limit |
| Current link use | 139 MB/s out + 56 MB/s in = **~16% of 10 Gb** | not the limit |
| CPU | load average **1.68** on a 40-thread box | not the limit |
| Chunk size | rclone **96 MB** vs B2's recommended **100 MB** | already right |
| `--transfers` | **20** on the Subjects task | already raised |
| Bandwidth limit | `bwlimit []` | none set |
| WAN path capacity | **128 MB/s sustained for 2.8 h on 2026-08-10** | proven ≥ 1 Gbps |

That last row matters most. The path has already carried 1,305 GB in a 2.8-hour window. If that
rate ran continuously it would be **11 TB/day**, roughly four times what the compression campaign
produces. Whatever is limiting us, it is not the size of the pipe.

## What the bottleneck actually is

### 1. Duty cycle — the sync is idle about half the time

Reading B2's own upload timestamps for all 182 uploaded objects:

```
aggregate: 14.91 TB over 96.1 h of active window = 43 MB/s
elapsed wall clock 2026-08-05 05:08 -> 2026-08-13 10:46 = 197.6 h
=> duty cycle 49%
```

Per-day, the throughput while actually running swings widely — 19, 23, 39, 50, 56, 69 and once
**128** MB/s. The task is scheduled **22:00 daily**; a run that ends early then waits up to a
full day for the next slot. The longest observed idle gap was **67.8 hours** (2026-08-08 to
2026-08-10, over a weekend).

At its own average rate run continuously, the sync would do **3.73 TB/day** — more than the
~2.9 TB/day the widefield campaign produces. **Duty cycle alone is enough to close the gap.**

### 2. It is an honest FIFO backlog, not starvation of part of the tree

I first suspected rclone's lexical walk was starving the end of the alphabet, because the missing
files skew heavily towards `ZYE_*`. That turned out to be **wrong**, and the check is worth
recording. Grouping all 302 `.wfz` by the day they were created:

```
2026-08-03   12/ 12  ##############################
2026-08-04    9/  9  ##############################
2026-08-05   16/ 16  ##############################
2026-08-06   32/ 32  ##############################
2026-08-07   22/ 22  ##############################
2026-08-08   28/ 29  ############################..
2026-08-09   36/ 40  ###########################...
2026-08-10   14/ 35  ############..................
2026-08-11   13/ 40  #########.....................
2026-08-12    0/ 50  ..............................
2026-08-13    0/ 17  ..............................
```

That is a clean monotonic frontier at around 2026-08-11 — exactly what a FIFO queue that cannot
keep up looks like. The lexical gradient (50/50 down to 11/51 across six buckets) is a much
softer signal and is confounded: the compressor runs largest-first, and subject identity
correlates with file size. **Creation date explains the data; sort position does not.**

The practical consequence is reassuring: no file is being permanently skipped. The backup
converges once production stops.

### 3. Interrupted runs throw away all in-flight work, and B2 bills for the debris

The bucket holds **20 unfinished large files**, and the bucket lifecycle rule has
`daysFromStartingToCancelingUnfinishedLargeFiles: null` — **they are never cancelled, and B2
charges for the stored parts indefinitely.**

Twenty is exactly `--transfers 20`. That is the signature of a run killed with every slot full,
which is what happened when the server was choked on 2026-08-13. rclone does not resume a B2
large-file upload across invocations, so **every interruption discards up to 20 files' worth of
partial transfer** — with ~66-84 GB files, potentially over a terabyte of wasted upload.

Of the 20:

- **4 are `widefield.wfz.partial-<pid>`** — our own temporary files. rclone caught them
  mid-write, began uploading them, and then the compressor renamed them away. That upload
  bandwidth was spent on files that no longer exist, and the parts are still being billed.
- **16 are genuine `.wfz`** cut off in flight. All 16 are still missing from B2.

The Subjects task has `exclude: []`, so nothing stops rclone from picking up temp files.

### 4. Hypothesis worth testing: the tree walk itself

`/mnt/data/data/Subjects` is not just the widefield data — it is the whole ~200 TB tree including
all the raw ephys. A `SYNC` re-walks the entire local tree and lists the entire remote prefix on
every run, before transferring anything. For scale, a bare metadata `find` over that tree took me
**~27 minutes** cold. If listing is eating a large slice of each run, it would depress the
effective duty cycle exactly as observed.

This is a hypothesis, not a measurement — I cannot see rclone's own logs (no `/var/log/jobs`,
nothing matching rclone in `/var/log`, no `cloud_sync` entries in `middlewared.log`). The admin
can confirm it instantly from the Cloud Sync task log in the TrueNAS UI: if there is a long delay
between "started" and the first transfer, this is it.

## What could be done, in order of expected payoff

Nothing below has been done. Items 1-2 need only the Backblaze web console; 3-5 need the TrueNAS
Cloud Sync UI (the admin, or whoever owns the task).

1. **Add a bucket lifecycle rule cancelling unfinished large files.** See the box below — the
   web console does not expose this field, and as of 2026-08-18 there are no orphans left to
   clear, so this is now a guard against future interruptions rather than a cleanup.

> ### `daysFromStartingToCancelingUnfinishedLargeFiles`: where the setting is, 2026-08-18
>
> **The B2 web console does not expose it.** The Lifecycle Settings dialog offers only four radio
> options — keep all versions / keep only the last / keep prior versions for N days / use custom
> lifecycle rules — and the separate "Unfinished Large Files" dialog only *lists* them and links to
> Browse Files to delete them by hand. Neither surfaces the field. If "Use custom lifecycle rules"
> opens a rule editor, it is worth a look, but the field appears to be API-only.
>
> **So it has to be set through the API or CLI, with a key that has `writeBuckets`.** The key
> configured on this workstation is read-only by design and cannot do it. Create an application key
> with write access to `sahalebackup` (or use the master key) in the console, authorize with it,
> then:
>
> ```powershell
> b2 bucket update sahalebackup --lifecycle-rule '{"daysFromHidingToDeleting": 30, "daysFromStartingToCancelingUnfinishedLargeFiles": 3, "daysFromUploadingToHiding": null, "fileNamePrefix": ""}'
> ```
>
> **The trap, and the CLI's own help says so: "All bucket lifecycle rules are set at once, so if
> you want to add a new rule, you need to provide all existing rules."** Omit
> `"daysFromHidingToDeleting": 30` from that JSON and the 30-day undo window is silently lost.
> Verify afterwards with `b2 bucket get sahalebackup`, which the read-only key can do.
>
> **Why 3 days and not 1.** A value of 1 would also work and the earlier draft of this doc said 1,
> but a legitimately slow upload is the failure mode to avoid: a 200 GB `.wfz` at the 4 MB/s this
> sync has been observed to hit would take 14 hours, and the 2026-08-13 wedge held transfers open
> for over six. 3 days keeps a wide margin, still fixes the leak completely (orphans currently
> persist forever), and costs almost nothing extra.
>
> **There is nothing to clean up right now.** The 20 orphans described below are gone. A listing on
> 2026-08-18 at 16:28 UTC showed 16 unfinished large files, **every one created between 15:43 and
> 15:53 UTC that same morning** and none of them a `.partial-*` temp file — that is a sync actively
> running, not debris. Do not cancel those by hand. Re-check with
> `b2 file large unfinished list b2://sahalebackup` and look at the `_dYYYYMMDD_mHHMMSS_` stamps in
> the file ids before deciding anything is stale.

2. **Check the bucket's total size in the B2 console.** Relevant to planning: the Subjects task
   backs up *everything*, so the ~95 TB of raw ephys `.bin` should already be up there. When the
   ephys campaign writes `.cbin` alongside the `.bin`, B2 gains ~37 TB of *additional* data until
   the originals are deleted — roughly **$257/month** extra during the transition at
   $6.95/TB/month. Worth knowing before starting that campaign.

3. **Exclude `*.partial-*` from the Subjects task.** Stops rclone uploading temp files that are
   about to be renamed away. No risk, immediate bandwidth saving, and no more temp-file orphans.

4. **Run the task more often than once a day** — every 6 hours, say. TrueNAS will not start a
   second run while one is going, so a more frequent schedule simply means "restart sooner after
   it stops". This is the single biggest lever: it attacks the 49% duty cycle directly.

5. **Consider `--b2-upload-concurrency 8` in the task's `args` field** (currently empty; rclone
   1.57's default is 4). Speculative — it only helps if per-connection throughput is the limit,
   which we have not established. Memory cost is chunk x transfers x concurrency =
   96 MB x 20 x 8 = ~15 GB of 273 GB, so it is affordable. Try it after 3 and 4, and only if
   those are not enough.

Note that rclone here is **v1.57.0-DEV** (late 2021), whatever TrueNAS 13.0-U6.8 ships. Upgrading
is not something to do on a production appliance for this.

## The arithmetic, if nothing changes

- Widefield produces ~2.9 TB/day of `.wfz`; B2 absorbs a median 1.51 TB/day. The backlog grows
  by ~1.4 TB/day while the campaign runs.
- Compression has ~9.3 days left, so the backlog peaks near **35 TB**.
- After that, production stops and the backlog drains at 1.5-3.7 TB/day: roughly **10 to 23 days**
  to reach a complete offsite copy.

This matters because deleting an original `.tar` is gated on its `.wfz` being offsite. **The
backup, not the compression, is now the critical path to reclaiming space.**
