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

## Recommendation, superseded — see below

The original conclusion was: build the ephys driver now, run it after, because a second job on
this workstation gets 20 MB/s. That still holds **for running it from this workstation**.

## What changed: run it on sahale instead

The machine turns out to be far better provisioned than assumed, and already has what mtscomp
needs:

```
Intel(R) Xeon(R) Silver 4210R    hw.ncpu 40    hw.physmem 273 GB
numpy 1.22.4 on Python 3.9.18
```

Two Xeon Silver 4210R: 20 physical cores, 40 threads, against this workstation's 16. Slower per
thread, considerably more of them. And mtscomp's dependencies are numpy (present), zlib (stdlib)
and tqdm (pure Python, installable to `~/.local` with no compiler and no root).

That removes the thing that made the answer "no":

| | from this workstation | on sahale |
|---|---|---|
| reads 94.79 TB over | **SMB, ~20 MB/s while the campaign runs** | the local pool |
| CPU | competes for 16 cores with the widefield job | 40 threads of its own |
| protocol overhead | one round trip per read | none |
| dependencies | fine | numpy already there |

The two campaigns would still contend for **the same disks** — that constraint does not go away.
But they would no longer contend for CPU, for the network, or for the SMB layer, and the ephys
reads become local rather than a ninth remote stream fighting eight others.

**This is worth testing before assuming it works.** The test is small: compress a few GB of a real
`.ap.bin` on sahale and time it. Everything below is read-only apart from writing a `.cbin` into a
scratch directory in your home.

```bash
# one-time, into your home directory only - no root, nothing compiled
python3.9 -m pip install --user mtscomp
```

```bash
mkdir -p ~/mtstest && cd ~/mtstest
SRC=/mnt/<pool>/Subjects/AL_0039/2025-09-30/6/p0_g0_t0.imec0/p0_g0_t0.imec0.ap.bin
dd if="$SRC" of=sample.ap.bin bs=1m count=4000
time python3.9 -m mtscomp sample.ap.bin sample.cbin sample.ch -n 385 -s 30000 -d int16
ls -l sample.*
```

`ls /mnt` gives the pool name for the `SRC` line. The 4 GB `dd` and the `.cbin` land in your home
directory; delete `~/mtstest` afterwards.

What the timing tells us: if mtscomp reaches even 100–200 MB/s there, the whole ephys corpus is
**one to two weeks** rather than the 55 days it would take over SMB from here — and it can run
concurrently with the widefield campaign instead of after it.

## The space argument still applies

`Y:` has 114.5 TB free. Both campaigns to completion before any deletion: ~32 TB more `.wfz` plus
~37 TB of `.cbin` is ~69 TB still to write, leaving ~46 TB. Running them concurrently reaches that
low-water mark sooner, which is an argument for getting the deletion of at least the verified
widefield tars moving — and that is gated on the B2 backlog.
