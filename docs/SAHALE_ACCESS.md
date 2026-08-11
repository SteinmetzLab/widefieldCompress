# Would admin access on sahale help? Mostly for one thing, and it is the important one.

## What we established earlier

From `docs/RUNNING_THE_BULK_JOB.md` and `docs/BACKUP_AND_RETENTION.md`:

| | |
|---|---|
| what sahale is | **TrueNAS 13.0-U6.8 on FreeBSD 13.1** |
| interpreter available | **Python 3.9.18 only** |
| your access | SSH yes; **no `sudo`**, no user `crontab` |
| cron contents | nothing but `at` and **`middlewared`** — the TrueNAS middleware |
| `rclone.conf` | **absent**, because the config lives in the TrueNAS database `/data/freenas-v1.db`, not a file |

So the B2 backup is a **Cloud Sync Task**: configured in the TrueNAS web UI, executed by
`middlewared`, using rclone underneath. That is why there is no config file to edit and no cron
entry to inspect.

The original plan *was* to run the compression on sahale. It was rejected on two grounds:

1. **It would not work.** `imagecodecs` — which provides the JPEG-LS codec — publishes no FreeBSD
   wheels. It would have to be built from source against charls, libjpeg-turbo, zstd and libaec,
   without root, on an appliance OS where package changes are wiped by the next update.
2. **It would not help much.** Compressing from local disk rather than over the share measured
   only **13% faster** (47.9 vs 41.8 MB/s). The bottleneck was CPU.

## What has changed since

Point 2 is now partly out of date, and honestly so. That 13% was a **single** archive. With eight
workers running, a ninth reader gets **~20 MB/s from the share against 645 MB/s when it is quiet**
— the disks are the constraint, not the CPU or the network.

But that cuts against running on sahale, not for it. Removing SMB does not remove disk contention:
it is the same pool either way. What running locally would save is the SMB protocol overhead on
eight concurrent streams, which is real but not the thirtyfold factor.

And there is an unknown that decides it: **nobody has checked what CPU sahale has.** A NAS is
often built with fewer, slower cores than a workstation. If it has eight modest cores it would be
slower than the 16-core machine doing the work now, regardless of where the data sits.

## So: three separate questions

### 1. Would admin let us fix the B2 sync? **Yes — and this is the one worth asking for.**

B2 is currently the pacing item for the entire deletion plan: uploading ~1.4 TB/day of `.wfz`
against ~2.4 TB/day being produced. Everything else is ready and waiting on it. With admin, in
**Data Protection → Cloud Sync Tasks**, someone can:

- raise rclone's **`--transfers`** (default 4) — B2 scales well with parallel uploads;
- look at the **task logs** and find out why 8 August moved one file and 9 August moved none;
- check for a **bandwidth cap** or a time-boxed window;
- confirm there are no **exclude patterns** quietly skipping things;
- confirm the **Transfer Mode is SYNC** (your own empirical test already showed deletions
  propagate, so this looks settled — but seeing it in the config would close it properly).

### 2. Would admin let us run the widefield compression there? **Not usefully.**

`imagecodecs` still has no FreeBSD wheels. Root makes building it *possible* rather than
*sensible*: it is an appliance, updates wipe package changes, and you would be making the file
server the build host for a scientific toolchain. The 16-core workstation already works.

### 3. Would it let us run **mtscomp** there? **Maybe — and this one is worth checking.**

mtscomp is the interesting case because its dependencies are pure Python plus numpy, zlib and
tqdm — none of the compiled image codecs that ruled out `wfcompress`. TrueNAS's own middleware may
already ship numpy. If it does, ephys compression could run on the box with no SMB layer at all.

Whether that is *faster* still depends on sahale's CPU, which is question one below.

## Cheap things to check over SSH, no sudo needed

```bash
uname -a; cat /etc/version 2>/dev/null
sysctl -n hw.model; sysctl -n hw.ncpu; sysctl -n hw.physmem
python3.9 -c "import numpy, zlib, sys; print('numpy', numpy.__version__, sys.version)"
midclt call cloudsync.query 2>&1 | head -20
```

Line by line: what it is and which release; **what CPU and how many cores** (the number that
decides whether running anything there is worth it); whether numpy is already present, which
decides whether mtscomp is even a candidate; and the middleware query for the sync task, which
will probably refuse without root but costs nothing to try.

## What I would actually ask the admin for

In priority order:

1. **Look at the Cloud Sync Task** — `--transfers`, logs for 8–9 August, any bandwidth cap.
   This is worth real money and it is blocking the deletion plan.
2. **Read-only access to the TrueNAS web UI**, if that is a thing they can grant. Most of what is
   needed here is *seeing* the configuration, not changing it.
3. Root shell — last, and only if mtscomp-on-sahale turns out to be attractive after the CPU
   check. It probably will not be.
