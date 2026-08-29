# Restarting the ephys compression without bricking the file server

Written 2026-08-28, after the widefield campaign finished. The question is how to run mtscomp over
94.64 TB of raw SpikeGLX without repeating 2026-08-13, when the same job took sahale off the
network for six hours and needed a remote reboot by the admin.

## What actually happened on 08-13, from the record

Nick's recollection is right: mtscomp was run **on sahale itself, 8 processes x 4 threads**, and
the server became unreachable. Two details from the logs are worth adding, because both change
what a safe retry looks like.

**The widefield campaign was running at the same time.** It was reading the same pool over SMB from
the workstation at 8 jobs x 4 threads. All sixteen output files across both campaigns stopped
growing within the same minute, 11:39, 34 minutes after ephys started. So 08-13 was *ephys plus
widefield*, not ephys alone. **We have never actually tested ephys by itself.**

**The benchmark that justified 8x4 was measured on 2 GB samples.** `EPHYS_COMPRESSION.md` records
8 processes x 4 threads at 138.9 MB/s and concludes "the knee has not been found; 10-12 processes
are worth trying". That extrapolation is unsafe, and here is why:

| | benchmark | real campaign |
|---|---|---|
| file size | 2 GB sample | 275-452 GB each |
| working set, 8 workers | ~16 GB | **2.2-3.6 TB** |
| sahale ARC | **209 GB** | 209 GB |
| result | 138.9 MB/s, fine | wedged in 34 min |

A 2 GB sample is served almost entirely from ARC. Eight concurrent multi-hundred-GB streams blow a
209 GB cache completely, so every read becomes a disk read - plus eight concurrent writes of the
`.cbin` output on the same pool. **The benchmark measured a cache-warm case and the campaign was
cache-hostile.** Load average 35 on a 40-thread box, with the box still answering TCP but no
application completing a request, is the profile of I/O starvation rather than CPU exhaustion.

Compounding it: the driver defaulted to `--largest-first`, so the first batch was the **eight
biggest files in the corpus**. That is the worst possible opening move for cache pressure, and it
also meant zero files had completed when things went wrong - no durable progress at all.

## The recommendation: run it from the workstation, not sahale

The decision to move mtscomp onto sahale was made because a second reader over SMB got **~20 MB/s**.
But that was measured *while the widefield campaign was saturating the share*. Widefield is done.
Measured 2026-08-28 with the pool quiet:

```
SMB sequential read, workstation -> sahale:  201 MB/s
```

**Ten times the number the original decision rested on.** That decision was correct for the
conditions it was made in, and those conditions no longer exist.

| | on sahale | on the workstation |
|---|---|---|
| throughput | ~139 MB/s measured (contended) | ~70-100 MB/s estimated |
| corpus time | ~8 days | ~11-18 days |
| CPU | 32 of 40 threads on the file server | 16 threads on a workstation |
| **failure mode** | **the lab loses its file server, admin reboot** | the workstation gets slow |
| recovery | needs someone with root, hours | `BelowNormal` priority, or just stop it |
| setup | staged files, no pip, no root | `mtscomp`, `numpy`, `tqdm` already installed |

**Roughly double the wall clock in exchange for a failure mode that cannot take the lab offline.**
There is no deadline on this work. Take the trade.

It also puts the job where the tooling already lives: the file log, the stop-file convention, the
supervisor pattern, and someone watching it.

## If you want sahale's speed anyway, here is how to test it safely

The governing principle, learned the hard way: **the abort path must not depend on the thing that
fails.** On 08-13 the overload killed SSH, which was the only way to stop the job.

1. **Add a stop-file check to `ephys_compress.py` first.** On the share, so it can be created over
   SMB from the workstation with no shell at all - `/mnt/data/data/temp/ephys_stop`. This is a
   prerequisite, not a nicety.
2. **Run `--smallest-first`.** Two reasons: checkpoints come in minutes rather than hours, and the
   stop file is only checked between files, so with 7-hour files an abort request could sit unread
   for most of a working day.
3. **Bound every test with `--max-tb`.** The driver already supports it.
4. **Build an external watchdog on the workstation** that times a small SMB stat against `Y:` every
   30 seconds and *writes the stop file automatically* when latency crosses a threshold. That is the
   dead-man switch, and it runs on the machine that is not the one failing.
5. **Ramp from below with evidence.** 2 processes, measure, 3, measure, 4. Never jump to 8. The
   08-13 configuration is the one known to fail; it is not a starting point.
6. **Watch ARC hit ratio, not just load average.** The failure is I/O, so
   `kstat.zfs.misc.arcstats.hits/misses` is the leading indicator. Load average moves late.

## Measured on the workstation, 2026-08-29

Two stages, smallest-first, `--below-normal`, over SMB. **372 files, 1.51 TB, zero failures.**
sahale's load average stayed at 1.13 throughout - the point of moving the compute here.

| | procs | hours | TB | aggregate | per worker |
|---|---|---|---|---|---|
| stage 1 | 2 | 6.20 | 0.500 | 22.4 MB/s | 11.9 MB/s |
| stage 2 | 4 | 7.72 | 1.005 | 36.2 MB/s | 10.0 MB/s |

**Scaling is sublinear**: doubling the processes gave 1.62x, and per-worker throughput fell from
11.9 to 10.0 MB/s. At 4 processes x 4 threads the machine's 16 logical cores are exactly
subscribed, so more processes means oversubscription and the per-worker figure should keep sliding.

### The compression ratio is materially worse than planned

This is the number that matters and it needs correcting twice over.

| measurement | ratio | basis |
|---|---|---|
| original estimate | **x2.56** | a single 4 GB *prefix* of one AP file |
| stage 1 headline | x2.907 | 86% LFP by volume - unrepresentative |
| stage 1, AP only | x2.15 | 34 small AP files, 0.067 TB |
| **stages 1+2, AP only** | **x1.94** | **150 files, 0.868 TB** |

The corpus is **92.88 TB of `.ap.bin`** against 2.13 TB of `.lf.bin`, so the AP figure is what
governs. At x1.94 rather than x2.56:

```
projected compressed size   48.8 TB   (was ~37 TB)
projected saving            46.2 TB   (was ~58 TB)
```

**About 12 TB worse than planned, and roughly $80/month less saved.** Still clearly worth doing -
46 TB is 46 TB - but the earlier number came from one 4 GB prefix and should not be quoted again.

Caveat in the other direction: smallest-first means everything measured so far is from the small
end (stage 2 median 7 GB against a corpus AP mean of 61 GB). Ratio is mostly a property of the
signal rather than the recording length, so this should hold, but it is worth re-checking once
larger files have been through.

## Is it CPU-bound or transfer-bound? Measured 2026-08-29: CPU, decisively

Both sides measured at once while the campaign ran at 6 processes x 4 threads:

```
ephys CPU          14.3 of 16 cores        90% of the machine
SMB read           46.8 MB/s
SMB write          20.4 MB/s
SMB total          67.2 MB/s               33% of the 201 MB/s the link gives
```

**The workstation is saturated and the network is not.** Confirmed by the scaling curve too:

| procs | aggregate | gain |
|---|---|---|
| 2 | 22.4 MB/s | |
| 4 | 36.2 MB/s | +62% |
| 6 | ~39 MB/s | **+8%** |

Six processes buy almost nothing over four, which is what hitting a CPU ceiling looks like. There
is no point going higher on this machine.

The SMB arithmetic is worth keeping: 67.2 MB/s of network for 39 MB/s of source is **1.7x**, made
up of the read (1.2x - the extra covers mtscomp re-reading the `.cbin` during its verify pass) and
the write (0.52x, matching the compression ratio). So each MB/s of compression costs ~1.7 MB/s of
SMB, and the ~134 MB/s of spare link supports roughly **79 MB/s more source throughput** before the
network becomes the constraint.

**The driver does not instrument this itself** - `elapsed_s` is wall time per file and nothing more.
These figures come from outside it: NIC byte counters and per-process CPU time sampled over the same
window. That is the cleaner way to measure it and I would not complicate the driver to reproduce it.

### So yes, more compute would scale - roughly 3x before SMB bites

Two places it could come from.

**sahale itself is the interesting one, because it reads locally and costs no SMB at all.** It has
40 threads against this machine's 16 and is currently idle at load 1.04. Even a modest 4 processes
there would add meaningfully without touching the link. The obvious objection is 2026-08-13 - but
that was ephys at 8x4 *while the widefield campaign was also hammering the same pool from the
workstation*, and widefield is finished. The stop file now exists too, which is what turned that
incident from recoverable into a reboot. Starting at 2-4 processes with the external watchdog
described above, and ramping on measurement, is a different proposition from what failed.

**Another lab machine over SMB** is the lower-risk option: there is headroom for roughly two more
workstations at the current rate, and the worst case is that machine getting slow.

**Either needs sharding first.** Two instances pointed at the same corpus would both select the same
files and duplicate the work - the `.partial-<pid>` naming stops them corrupting each other, but
nothing stops them racing. A `--shard i/n` option partitioning on a stable hash of the path would
fix it with no coordination between machines and no shared state beyond the run log.

Rough arithmetic: workstation ~39 MB/s alone gives ~28 days. Add sahale at 4 processes and the pair
might reach 80-100 MB/s, which is **11-13 days**.

## The two-machine setup, started 2026-08-29

Deliberately on a Saturday, when the lab is quiet and a mistake costs least.

| | shard | procs | priority | stop file |
|---|---|---|---|---|
| workstation | `0/2` | 6 x 4 | below normal | `D:\temp\ephys_stop` |
| sahale | `1/2` | **2** x 4 | `nice -n 10` | `/mnt/data/data/temp/ephys_stop` = `Y:\temp\ephys_stop` |

The shard split came out at **753 of 1,499 remaining files, 46.80 TB** for sahale - even, and
verified to agree between the two machines' different path forms before either was started.

**`scripts/sahale_watchdog.py` runs on the workstation**, probing how long a `stat` on `Y:\Subjects`
takes every 30 s. Three consecutive probes over 5 s and it writes sahale's stop file over SMB. That
placement is the whole design: on 08-13 the overload killed ssh, which was the only way to stop the
job, so the abort path must not depend on the machine that is failing. Confirmed `Y:\temp` is
writable from the workstation before starting - without that the switch is decorative.

sahale went from load average 1.04 to 2.44 on a 40-thread box, which is the point of choosing 2
processes rather than the 8 that failed in August.

**To stop everything:** create both stop files. Either can be created from the workstation with no
shell on sahale. In-flight files finish rather than being killed, so nothing is thrown away, and
the drivers resume from their own logs.

## Regardless of where it runs

- **Clean up first.** Eight stale `.cbin.partial-*` from the 08-13 crash are still on the share,
  ~117 GB. `clean_partials()` in the driver removes them, or delete them by hand.
- **Expect ~2.56x**, measured twice on real data, so ~94.64 TB becomes ~37 TB. Note that while both
  copies exist the pool and the Backblaze bill carry the sum, roughly +37 TB until the `.bin`
  originals are deleted.
- **The deletion gate does not cover ephys.** `delete_tar.py` is widefield-specific. Deleting
  `.bin` files after compression needs its own equivalent, and mtscomp's own verify pass is the
  natural basis for it.
