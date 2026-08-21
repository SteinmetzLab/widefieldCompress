# Step 0: proving the Backblaze undo, before anything relies on it

**Status 2026-08-17: the part that matters is done and it passed.** Three sessions, nine checks,
no failures. Run it yourself with `scripts/b2_restore_test.py`. Two findings below change the
deletion plan, one of them materially.

---

## The test was split in two, and only one half needs a deletion

The original plan here was: delete a tar, wait for the sync, restore it from B2's prior-version
history, hash it. That conflates two questions, and only the second needs anything destructive:

1. **Is the offsite copy intact, complete, and able to reconstruct the original data?**
   Answered by downloading it and hashing it. No deletion required.
2. **Does B2's prior-version window return a file after a delete propagates?**
   This genuinely needs a delete, a sync cycle, and a restore.

Doing (2) before (1) would have been backwards — deleting a copy on the strength of an offsite
backup nobody had ever read back. (1) is now done.

## What was run

`scripts/b2_restore_test.py --session <S> --bucket sahalebackup`, three checks per session:

| | check | what a pass means |
|---|---|---|
| **A** | the `widefield.tar` in B2 hashes to the receipt's `source_tar_sha256` | the offsite original is intact and restorable *today*, without any deletion |
| **B** | the `widefield.wfz` in B2 is byte-identical to the server's | the sync transferred the replacement correctly, not just a file of the right size |
| **C** | the *downloaded* `.wfz` rebuilds `source_tar_sha256` | the offsite replacement is a working archive on its own, independent of the server |

C is the one that matters for deletion. A and B are what make C interpretable: without B, a pass
on C would only prove the *local* file is good.

## Results — 9 of 9 passed

| session | .wfz | shift | TIFF | shape | A | B | C |
|---|---|---|---|---|---|---|---|
| `AL_0033/2025-03-17/1` | 0.68 GB | 0 | no | 560x560 | PASS | PASS | PASS |
| `ZYE_0035/2021-07-17/1` | 0.72 GB | 4 | yes | 512x512 | PASS | PASS | PASS |
| `AL_0048/2026-06-11/4` | 1.28 GB | 0 | no | 560x560 | PASS | PASS | PASS |

Chosen to cover both code paths that exist among the 212 `SAFE` sessions — `shift=0` little-endian
raw (19 sessions) and `shift=4` big-endian TIFF (193 sessions) — two frame shapes, three subjects,
and source archives written between 2021 and 2026.

Download ran at 53-96 MB/s while the compression campaign was running, so pulling data back out of
B2 is not a bottleneck. A full-corpus restore of all 24 TB would be roughly 3 days at that rate.

**This is a sample, not a proof over the corpus.** Three of 326. What it establishes is that the
sync produces faithful copies and that the `.wfz` format survives a network round trip — not that
every one of the 326 objects is good. Condition 7 of the gate still only checks name and size; if
you want per-file certainty before a large deletion batch, run this script over the batch.

## Finding 1: the retention window is 30 days, not 60

`BACKUP_AND_RETENTION.md` and `DELETION_PLAN.md` both state that B2 keeps prior versions for
**60 days**, taken from the bucket's Lifecycle Settings in the web UI. The bucket's actual rule,
read back from the API on 2026-08-17:

```json
"lifecycleRules": [
    {
        "daysFromHidingToDeleting": 30,
        "daysFromStartingToCancelingUnfinishedLargeFiles": null,
        "daysFromUploadingToHiding": null,
        "fileNamePrefix": ""
    }
],
"revision": 4
```

**`daysFromHidingToDeleting` is 30.** Once a deletion reaches B2, the prior version survives
**one month, not two**. The bucket is on revision 4, so this was plausibly changed at some point
after that doc was written; either way, 30 is what is live now.

What this changes:

- **Halve the review window.** `DELETION_PLAN.md` step 4 says no batch is deleted whose 60-day
  window would close unreviewed. That budget is 30 days.
- **Halve the retention cost estimate.** `BACKUP_AND_RETENTION.md` puts the one-time cost of the
  retention lag at ~$870. At 30 days it is ~$435.
- The recommendation in that doc to *leave retention at 60 days* during the deletion campaign is
  now a recommendation to *raise it to 60*, which is a different and more deliberate act. Worth a
  decision rather than an assumption.

`daysFromUploadingToHiding: null` is good news and unchanged: nothing expires while it is live on
the server.

## Finding 2: the unfinished-large-file leak is still open

`daysFromStartingToCancelingUnfinishedLargeFiles` is still `null`, so the 20 orphaned partial
uploads described in `B2_THROUGHPUT.md` are still being billed with no expiry. Fixing it is a
one-line lifecycle change in the B2 console and cannot affect any completed object.

## What is still untested

**Whether a delete actually propagates to B2 as a hide rather than a hard delete, and whether the
prior version comes back.** That needs:

1. delete one tar on the server (a `SAFE` row from `data/deletable_audit.csv`);
2. wait for the nightly 22:00 sync;
3. `b2 ls --versions b2://sahalebackup/subjects/<S>/` and confirm the object is *hidden*, not gone;
4. download that version by id and hash it against `source_tar_sha256`.

Steps 3 and 4 work with the existing read-only key. **Step 1 does not** — the key has no delete
capability, by design, and deleting on the server is a decision, not a task. Nobody should run
step 1 without deciding it explicitly.

Given A/B/C all pass, this is now a third-line check rather than a blocker: for any session that
passes C, the recovery path is the `.wfz`, which exists in two places and is proven to rebuild the
original. The B2 tar prior-version is the backstop behind the backstop. It is still worth doing
before a large batch, because `BACKUP_AND_RETENTION.md` flags an unresolved question that matters
more than the retention days: **whether the Cloud Sync task is in SYNC or COPY mode.** In COPY
mode a server-side delete never reaches B2 at all, the prior-version question is moot, and the
offsite copy simply persists.

## Independent of B2 entirely

Condition 8 of the gate — re-hash the tar on disk and confirm it equals what the `.wfz` rebuilds —
needs no Backblaze. `scripts/audit_deletable.py --strict --rehash-tar` does it. Slow, since it
reads both files in full, but it is the strongest per-archive statement available.
