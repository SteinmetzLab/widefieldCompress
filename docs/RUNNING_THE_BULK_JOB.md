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

Returns flatten past 8 jobs. Going faster from here would mean cutting the per-frame numpy passes
in the codec, which is not worth it at this scale.

**Expected wall clock for Y: (120.7 TB): ~16 days continuous** at 86.7 MB/s, versus ~50 days at the
old single-process default. Spread over nights and weekends, budget a couple of months.

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
python -c "from wfcompress.lab.census import scan; scan().write_csv('census.csv')"
```

Then start the batch. The log is JSONL, one line per session, and the driver skips anything already
marked `ok`, so it is safe to kill and restart at any point.

```bash
python -m wfcompress.lab.batch --census census.csv --server Y \
    --jobs 8 --threads 4 --log bulk.jsonl
```

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
