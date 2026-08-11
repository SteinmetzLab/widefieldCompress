# Can the ephys compression run alongside the widefield one?

**Measured answer: no, not usefully — and the reason is not the one I expected.**

## The CPU has room. That is a red herring.

With eight widefield workers running, `--jobs 8 --threads 4`:

| | |
|---|---|
| logical cores | 16 |
| held by the campaign | **10.2** |
| total system load | 78% average (50–93%) |

So roughly five cores idle. On that basis a second job looks free.

## The share does not have room

The share is the shared resource, and it is close to saturated. A single sequential reader,
16 MB blocks, reading from the middle of a large `.ap.bin` while the campaign runs:

| | |
|---|---|
| second reader, campaign running | **~20 MB/s** (21.5 and 19.7 over two 45 s passes) |
| same link, campaign stopped | **645 MB/s** (measured earlier) |

**A thirtyfold reduction.** Note the campaign's own SMB traffic peaks around 165 MB/s — about 13%
of a 10 GbE link — so this is not the network running out. It is sahale's pool: eight concurrent
large sequential reads plus eight writes is enough to have the disks fully committed, and a ninth
stream gets the scraps.

## What that means in days

Ephys is 94.79 TB to read.

| | rate | elapsed |
|---|---|---|
| started now, alongside widefield | ~20 MB/s per stream | **~55 days**, and it slows widefield too |
| started after widefield finishes | I/O no longer the limit; mtscomp becomes CPU-bound | **~2 weeks** |

Running both does not create throughput, it splits it. Total bytes processed per day is roughly
unchanged; what changes is that both finish later and there are two campaigns to babysit instead
of one. There is also no complementarity to exploit — mtscomp is zlib, so it wants CPU too, not
some other resource the widefield job leaves idle.

## The space argument points the same way

`Y:` has 114.5 TB free. If both campaigns run to completion before anything is deleted:

| | |
|---|---|
| widefield `.wfz` still to write | ~32 TB |
| ephys `.cbin` would add | ~37 TB |
| **still to write** | **~69 TB** |
| free afterwards | ~46 TB, i.e. 88% full |

Survivable, but it removes the slack that has been useful every time something went wrong.

## Recommendation

**Build the ephys driver now; run it after.** Writing and testing the mtscomp batch harness costs
nothing that the campaign needs — it is a few hundred lines and a handful of test files — and
having it ready means the ephys run can start the day the widefield one ends, rather than starting
to be written then.

The one thing that would change this: **another machine with its own path to the data.** mtscomp's
dependency profile is unusually portable — pure Python, numpy, zlib, tqdm, with none of the
compiled image codecs that made `wfcompress` impossible to run on the FreeBSD box. But it would
still be reading the same sahale pool, so a second machine helps only if the bottleneck is this
workstation, and the measurement above says it is not.
