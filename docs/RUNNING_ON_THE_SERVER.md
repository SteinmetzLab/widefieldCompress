# Running wfcompress on the server

Running on the server removes the network from the hot path entirely. The workstation has to pull
every byte over SMB, compress it, push the result back, then pull it again to verify — for the full
Y: corpus that is ~306 TB of network traffic. On the server itself it is local disk I/O, and the job
becomes purely CPU-bound.

Everything below is read-only until the last section.

## 1. Check what you have

```bash
ssh <you>@sahale.biostr.washington.edu
python3 --version          # need >= 3.10
nproc                      # how many workers you can run
free -g
df -h /data                # confirm the share and its free space
```

`imagecodecs` ships manylinux wheels for x86_64, so nothing should need compiling. If `python3` is
older than 3.10, check for a newer one (`ls /usr/bin/python3.*`) or use conda/pyenv.

## 2. Install into a virtualenv

Keep it off the share — a venv on network storage is slow and gets backed up for no reason.

```bash
python3 -m venv ~/wfc-venv
~/wfc-venv/bin/pip install --upgrade pip
~/wfc-venv/bin/pip install git+https://github.com/SteinmetzLab/widefieldCompress
~/wfc-venv/bin/wfcompress --help
```

## 3. Prove the round trip on one session before anything else

Pick a small one, work in scratch space, and touch nothing in the session folder:

```bash
mkdir -p /data/temp/wfCompressServerTest && cd /data/temp/wfCompressServerTest
SRC=/data/Subjects/FD_010/2026-02-23/3/widefield.tar

cp "$SRC" original.tar
~/wfc-venv/bin/wfcompress compress   original.tar test.wfz
~/wfc-venv/bin/wfcompress decompress test.wfz     restored.tar
~/wfc-venv/bin/wfcompress verify     original.tar restored.tar     # expect IDENTICAL
```

Expect roughly 2.9× on that session. Compare the MB/s figures with the workstation's 68 MB/s
compress / 91 MB/s decompress to see what the network was costing.

Then find the thread count that saturates the box — JPEG-LS scales close to linearly until it runs
out of memory bandwidth:

```bash
for t in 4 8 16 32; do
  echo "== $t threads"
  /usr/bin/time -f "%e s" ~/wfc-venv/bin/wfcompress --threads $t compress original.tar /dev/null 2>&1 | tail -2
done
```

## 4. Run the pilot list

```bash
~/wfc-venv/bin/python -m wfcompress.lab.census --roots /data/Subjects --out census.csv   # re-scan
~/wfc-venv/bin/python -m wfcompress.lab.batch \
    --census census.csv --server Y --limit 10 \
    --out-dir /data/temp/wfCompressServerTest \
    --threads 16 --log pilot.jsonl
```

`--out-dir` keeps outputs out of the session folders. Drop it to write each `.wfz` beside its tar.
**Originals are never deleted** — there is no delete path in the tool at all; removal is a separate,
manual step once you have read the receipts.

## 5. Bulk run

Use `tmux` or `nohup` so it survives the SSH session, and be a good citizen about load:

```bash
tmux new -s wfc
~/wfc-venv/bin/python -m wfcompress.lab.batch \
    --census census.csv --server Y --largest-first \
    --threads 16 --log /data/temp/wfc/bulk.jsonl
# detach with ctrl-b d ; reattach with: tmux attach -t wfc
```

The log is JSONL, one line per session, and the driver skips sessions already marked `ok`, so it is
safe to kill and restart. To keep it off the share during working hours:

```bash
nice -n 15 ionice -c3 ~/wfc-venv/bin/python -m wfcompress.lab.batch ...
```

Progress and results:

```bash
wc -l /data/temp/wfc/bulk.jsonl
jq -s 'map(select(.ok)) | {n: length,
        in_TB:  (map(.source_bytes) | add / 1e12),
        out_TB: (map(.output_bytes) | add / 1e12),
        ratio:  ((map(.source_bytes) | add) / (map(.output_bytes) | add))}' /data/temp/wfc/bulk.jsonl
jq -r 'select(.ok | not) | "\(.session)\t\(.error)"' /data/temp/wfc/bulk.jsonl
```

## 6. Deleting originals — separate, deliberate, and last

Only after the receipts have been reviewed. For each session the receipt records `byte_identical:
true` and the SHA-256 of both the original tar and the restored one. A one-liner that deletes only
where that is proven:

```bash
jq -r 'select(.ok and .byte_identical) | .tar' /data/temp/wfc/bulk.jsonl > verified.txt
wc -l verified.txt
# review it, then:
# xargs -a verified.txt -d '\n' rm -v --
```

Before running that, read `docs/BACKUP_AND_RETENTION.md` — deleting from the share does **not**
immediately reduce the Backblaze bill, and depending on the lifecycle rules it may never do so
without a configuration change.
