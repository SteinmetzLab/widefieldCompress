# Running the bulk job

## Where to run it: the Windows workstation

Not sahale. That was the original plan and it was wrong on both counts:

- **It won't work there.** sahale is TrueNAS 13.0-U6.8 on **FreeBSD 13.1**, with Python 3.9.18 and
  no other interpreter. `imagecodecs` publishes no FreeBSD wheels, so it would have to be built
  from source against charls, libjpeg-turbo, zstd and libaec — without root, on an appliance OS
  where package changes are wiped by updates.
- **It wouldn't help much anyway.** Compressing the same file from local disk rather than the share
  is only **13 % faster** (47.9 vs 41.8 MB/s). The network was never the bottleneck. Measured at
  the recommended settings below, SMB traffic peaks around 165 MB/s — about **13 % of the 10 GbE
  link**. The limit is CPU.

The workstation has 16 logical cores and a 10 Gb link to sahale, which is enough.

## Settings

```bash
python -m wfcompress.lab.batch --census census.csv --server Y --jobs 8 --threads 4
```

`--jobs` runs whole sessions concurrently in separate processes; `--threads` parallelises frames
within one session. **Jobs matter far more than threads**, because `jpegls_encode` releases the GIL
but the numpy work around it does not, so extra threads inside one process contend. Measured on
8 real sessions (23.02 GB), doing the full workload — compress *and* streaming verification,
reading and writing the share:

| jobs | threads | workers | MB/s | vs default |
|---|---|---|---|---|
| 1 | 8 | 8 | 27.7 | 1.00x |
| 1 | 16 | 16 | 37.8 | 1.37x |
| 4 | 4 | 16 | 75.9 | 2.74x |
| 4 | 8 | 32 | 79.7 | 2.88x |
| 6 | 4 | 24 | 74.4 | 2.69x |
| 8 | 2 | 16 | 83.3 | 3.01x |
| **8** | **4** | **32** | **86.7** | **3.13x** |

Returns flatten past 8 jobs. `--jobs` costs memory roughly linearly - about 1.2 GB per worker on
the largest archives - so 8 jobs is ~9 GB, comfortable on 64 GB. Going faster from here would mean cutting the per-frame numpy passes
in the codec, which is not worth it at this scale.

### What the staged pilot actually did

588.2 GB over 13 archives, every one rebuilt byte-for-byte, in two stages:

| stage | arrangement | size | time | MB/s |
|---|---|---|---|---|
| A: 12 sessions, 1-32 GB | 8 jobs x 4 threads | 158.1 GB | 0.67 h | 65.1 |
| B: the 430 GB archive alone | 1 job x 16 threads | 430.1 GB | 3.50 h | 34.1 |

**Expected wall clock for Y: (120.68 TB): 16-22 days continuous.** The spread is real and worth
understanding before you plan around it:

- **16.1 days** at the 86.7 MB/s the sweep measured with a full queue of 8 concurrent sessions.
- **21.5 days** at stage A's 65.1 MB/s. That figure is depressed by a tail effect, not by anything
  fundamental: with only 12 items ranging 1-32 GB, the four largest ran nearly the whole window
  while the rest had finished, so for most of the run fewer than 8 workers were busy. A
  1,120-session queue stays full, so the real rate should sit near the sweep.
- **41 days** if it were run one session at a time, which is the floor, not a plan.

**Archive size does not cost throughput.** The 430 GB archive ran at 34.1 MB/s on 16 threads
against 37.8 MB/s measured for a 3 GB one at the same thread count, and its peak memory was
**1.15 GB** on a 64 GB machine - the largest session in the corpus, 679,645 frames. Shells are
identical across frames and get interned to one copy, so the multi-GB memory growth that looked
possible does not happen.

Spread over nights and weekends, budget one to two months of calendar time.

## Being a good neighbour

32 worker processes hammering the share will be noticed. The job is CPU-bound, so throttling costs
little:

```bash
# fewer jobs during working hours
python -m wfcompress.lab.batch --census census.csv --jobs 3 --threads 4
```

On Windows, start it at reduced priority so interactive work stays responsive:

```bash
start /low /b python -m wfcompress.lab.batch --census census.csv --jobs 8 --threads 4
```

## Running it

Re-run the census first — it is a snapshot and new sessions land continuously:

```bash
python scripts/regenerate_census.py      # writes data/census_Y.csv
```

The current one is committed as `data/census_Y.csv`: 1,126 tars, 1,120 widefield, 120.68 TB, and
18 archives (1.68 TB) whose geometry cannot be resolved from the session folder. Those 18 need
`--assume-shape 560 560`, which only ever applies where geometry is otherwise unknown - see
`PLAN.md` section 7a for why 560x560 is the right answer for them.

Then start the batch. The log is JSONL, one line per session, and the driver skips anything already
marked `ok`, so it is safe to kill and restart at any point.

```bash
python -m wfcompress.lab.batch --census data/census_Y.csv --server Y \
    --jobs 8 --threads 4 --assume-shape 560 560 \
    --log bulk.jsonl --file-log fileEditLog.csv
```

`--file-log` appends a row for every file created, replaced or removed, with path, size, UTC
timestamp and event type. That is the record to audit inside the 60-day window in which the share
can still recover a deleted version. Temporary files from atomic writes appear in it too; they are
counted separately in the summary so they cannot be mistaken for real deletions.

Progress:

```bash
python - <<'EOF'
import json, pathlib
rows = [json.loads(l) for l in pathlib.Path("bulk.jsonl").read_text().splitlines()]
ok = [r for r in rows if r.get("ok")]
tin = sum(r["source_bytes"] for r in ok); tout = sum(r["output_bytes"] for r in ok)
print(f"{len(ok)}/{len(rows)} ok   {tin/1e12:.2f} TB -> {tout/1e12:.2f} TB   x{tin/max(tout,1):.2f}")
print(f"byte-identical: {sum(bool(r.get('byte_identical')) for r in ok)}/{len(ok)}")
for r in rows:
    if not r.get("ok"):
        print("FAILED", r["session"], r["error"][:100])
EOF
```

## Deleting originals — separate, manual, and last

There is deliberately **no delete flag in the tool**. Deletion is a decision made from the receipts
after reviewing them, not a side effect of a run.

Each session's `widefield.wfz.receipt.json` records `byte_identical: true` plus the SHA-256 of the
source archive. To build a list of provably safe deletions:

```bash
python - <<'EOF'
import json, pathlib
rows = [json.loads(l) for l in pathlib.Path("bulk.jsonl").read_text().splitlines()]
safe = [r["tar"] for r in rows if r.get("ok") and r.get("byte_identical")]
pathlib.Path("verified.txt").write_text("\n".join(safe))
print(len(safe), "archives verified byte-identical")
EOF
```

Review that list, then delete in tranches. Hold the first ~50 sessions' originals until you have
read their receipts.

**Before deleting anything, read [BACKUP_AND_RETENTION.md](BACKUP_AND_RETENTION.md).** Deleting from
the share does not immediately reduce the Backblaze bill — old versions persist for 60 days — and
the compressed files are new objects that get backed up alongside the originals in the meantime, so
the bill rises before it falls.
