# Deleting the original tars: a proposal

Nothing has been deleted. This document proposes how to do it, what it would cost, and what I
would want in place first. `wfcompress` still has **no delete path anywhere** — the batch driver
never had one, by design.

## The principle

> Delete nothing that has not been proven redundant **today**, and delete nothing you cannot undo
> tomorrow.

Two halves, and both matter. A receipt written three weeks ago proves the archive was good three
weeks ago. And "provably redundant" is worth much less if the proof turns out to be wrong and
there is no way back.

## What can actually go wrong

Not hypotheticals — these are the failure modes this corpus has already shown or come close to.

| | evidence |
|---|---|
| **The tar changes after compression.** | All 21 mixed archives were re-tarred days after being censused, going from 3.36 TB to 2.13 TB. If we had compressed and deleted on the old contents, the new contents would have been lost. |
| **A run dies between compressing and verifying.** | Happened on 7 August: three `.wfz` files were written and the workers died before the receipt. Complete-looking files, never verified. |
| **The output is written but truncated.** | Two crashes left 5 and 5 `.partial` files, 297 GB and 163 GB. Named differently, so harmless — but only because the atomic-write pattern was there. |
| **The `.wfz` rots after it is written.** | Not observed here, but it is exactly what a receipt from three weeks ago cannot rule out. |
| **The `.wfz` is not yet in the offsite backup.** | The B2 sync is nightly. Delete a tar the same day its `.wfz` was written and there is a window where the only copy is on one server. |
| **A format we can no longer read.** | Version-1 `.wfz` files predate `source_tar_sha256` entirely; they cannot prove byte-identity at all. |

## The gate

An original is a deletion candidate only if **all** of these hold at the moment of deletion:

1. **The run log records success** for that archive — the last success, if it was retried.
2. **A `.wfz` exists beside it**, of exactly the size the log recorded.
3. **Format version ≥ 2**, and it carries a `source_tar_sha256`. Not partial.
4. **The tar on disk is still the size that hash was taken over.** This is the guard that catches
   the "someone re-tarred it" case, and it is free.
5. **A receipt sits beside the `.wfz`** recording `byte_identical: true`.
6. **The `.wfz` reproduces that hash today** — a fresh `wfcompress check`, not a stored claim.
7. **The `.wfz` is present in Backblaze**, so deleting the tar never leaves a single copy.

Conditions 1–5 are metadata: seconds for the whole corpus, cheap enough to re-run before every
batch. 6 is a full read. 7 is a B2 API query per file.

Optionally, **8: the tar itself re-hashes to `source_tar_sha256`.** That is the only check that
closes the loop completely — it proves the bytes on disk right now are the bytes the `.wfz`
reconstructs, rather than inferring it from a size match. It costs a second full read.

## The cheap tier, run today

`scripts/audit_deletable.py`, metadata only, over everything compressed so far:

```
212 archives recorded as compressed (41.11 TB of tar)
SAFE to delete :   212 archives, 41.11 TB
REFUSED        :     0 archives, 0.00 TB
elapsed 680 s
```

Clean: no size drift on any tar, no missing or short `.wfz`, no version-1 files, no missing
receipts, every receipt claiming a byte-identical rebuild. Per-archive detail in
`data/deletable_audit.csv`.

That is the paperwork being consistent. It is *not* yet a licence to delete — conditions 6, 7 and
8 have not been run, and those are the ones that check the world rather than the records.

## What each tier costs

| tier | what it proves | reads | time |
|---|---|---|---|
| metadata (1–5) | the paperwork is consistent | ~nothing | minutes |
| + B2 presence (7) | there is an offsite copy | API only | minutes |
| + re-verify (6) | the `.wfz` still rebuilds the recorded bytes | 50 TB of `.wfz` | ~3 days |
| + re-hash tar (8) | the tar on disk *is* those bytes | +119 TB | ~+2.5 days |

Times assume the aggregate throughput the campaign is currently getting and that this runs
alongside it. Against permanently destroying 119 TB of irreplaceable raw data, **I would run the
full tier once**, and re-run the cheap tiers before each batch.

## Proposed rollout

**Step 0 — prove the undo before relying on it.** Pick one small tar. Delete it. Restore it from
Backblaze. Hash the restored file and confirm it equals `source_tar_sha256`. Only then is the
60-day window a safety net rather than an assumption. This is the single most valuable hour in the
whole plan, and it should happen before any batch.

**Step 1 — full audit, no deletion.** Run the strict tier over everything compressed so far.
Output is a CSV with one row per archive: verdict, reason, both hashes, both sizes. Read it.
Anything refused stays refused until a human looks at it.

**Step 2 — pilot batch.** Ten archives, smallest first, ~1 TB. Delete, then re-run the audit and
confirm the `.wfz` files still verify and B2 still holds the prior versions. Stop for a week.

**Step 3 — the bulk, largest-first, in batches of ~50** with the cheap gate re-run before each and
a hard cap on bytes per invocation. Largest-first because by then the process is proven and the
space is the point.

**Step 4 — nothing expires unreviewed.** No batch is deleted whose 60-day B2 window would close
before someone has looked at the audit for it.

## Excluded from the first pass

- **The 21 formerly-mixed archives.** Their history is unusual and their current `.wfz` files, if
  any, predate the trim. Re-compress and re-verify first.
- **`default` and `test` subjects**, until the lab confirms nobody wants them.
- **Any version-1 `.wfz`.** Rewrite first.
- **Anything the audit refuses**, for any reason, until the reason is understood.

## The audit trail

Everything already goes to `data/fileEditLog.csv` — timestamp, event, path, size, host, pid. For
deletions I would add a `data/deletions.csv` carrying, per row: the tar path and size, the `.wfz`
that replaced it, `source_tar_sha256`, the timestamp of the verification that authorised it, and
the B2 restore command for that exact object. That last column is what turns "we have a backup"
into "here is the command".

## What I would build

Small, and mostly guards:

- `wfcompress.lab.deletion` — the gate above (**written, and the read-only audit is runnable
  now**: `scripts/audit_deletable.py`).
- a B2 presence check, via the B2 CLI or API.
- `scripts/delete_originals.py` — dry-run by default; requires a fresh audit CSV no older than a
  chosen window; refuses to exceed a byte cap; refuses without an explicit confirmation token;
  logs every removal to both CSVs before removing anything.

## Recommendation

Do step 0 this week — it is one file and an hour, and it either validates the whole plan or tells
us the backup is not what we think. Run the full audit while the campaign finishes. Delete nothing
until the campaign is done, because until then the ratio and the failure list are both still
moving. Then pilot ten, wait a week, and proceed.

The saving does not go anywhere by waiting: it is ~$5,800/year for the widefield tars, and it will
still be there in a month.
