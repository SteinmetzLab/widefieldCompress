# Backblaze B2: what happens to the bill when we delete

Status: **answered, 2026-08-17.** Retention is 30 days (see §1). And the question that mattered
far more — whether deletions propagate to B2 at all — is now settled: **the `Subjects` task is
`transfer_mode: SYNC`**, so they do.

> **SYNC confirmed 2026-08-17**, read from `tasks_cloudsync` in `/data/freenas-v1.db` via
> `scripts/sahale_read_cloudsync.py`. Per task:
>
> | task | path | direction | mode |
> |---|---|---|---|
> | Backup to Backblaze - Subjects/ | `/mnt/data/data/Subjects` | PUSH | **SYNC** |
> | Backup to Backblaze - /Code | `/mnt/data/data/Code` | PUSH | SYNC |
> | Backup to Backblaze - Alyx-backup/ | `/mnt/data/data/alyx-backup` | PUSH | **COPY** |
>
> The modes differ per task, so check the right one: it is the `Subjects` task that governs the
> tars. Corroborating evidence from the bucket itself — when the lab trimmed
> `ZYE_0098/2025-12-17/2/widefield.tar` from 143.1 to 93.5 GB on 08-09, the sync uploaded the new
> version on 08-16 and **kept the old one as a prior version**. Both are billed until the 30-day
> clock expires.
>
> So a deleted tar will be hidden in B2, retained 30 days, then permanently removed, and billing
> stops at that point. That is the intended behavior and no B2-side deletion step is needed.

---

## 1. Retention: answered — ~~60~~ **30 days**

> **Updated 2026-08-17. This is now 30 days, and that is deliberate.** Nick changed the rule from
> 60 to 30 in anticipation of the tar deletions; the live value read back from the API is
> `daysFromHidingToDeleting: 30`, bucket-wide, at bucket revision 4. Nothing was misconfigured —
> the doc simply predates the change.
>
> Consequences: every "60 days" below and in `DELETION_PLAN.md` should be read as **30**, and cost
> figures derived from it halve (the ~$870 one-time retention lag becomes **~$435**). The
> recommendation in §2 to leave it at 60 is superseded by that decision.
>
> **Why not go to 1 day and reclaim the billing immediately?** Nick's reasoning, and it is right:
> the window is not there for *these* tars, whose redundancy is proven. It is there for the
> unrelated accident nobody has had yet — some other file deleted or corrupted by mistake
> elsewhere in the lab. Shrinking it to 1 day to save ~$435 once would remove the only protection
> against that class of event across the entire ~200 TB `Subjects` tree. 30 days is the
> compromise, and waiting 30 days for the billing to clear is fine.

The bucket's Lifecycle Settings were read as *"Keep prior versions for this number of days: 60"*,
i.e. a lifecycle rule of `daysFromHidingToDeleting: 60` over the whole bucket. **The API now
reports 30.**

So when a file is deleted on the server and that deletion reaches B2, the prior version is retained
for **30 days** and then permanently removed. Storage billing stops at that point, not at deletion.

**Consequence: savings lag deletion by one month.** That is the whole cost of this setting.

## 2. Should we shorten it for the widefield paths? No.

It is technically possible — B2 lifecycle rules are per-prefix and the CLI (`b2 bucket update
--lifecycle-rules '[...]'`) accepts an array of them, which the web UI's single-path box cannot
express. But it is not worth doing, for three reasons.

**The money is small.** At ~$6/TB/month:

| | |
|---|---|
| to delete after compression | 72.4 TB |
| cost of the 60-day lag | **~$870, one-time** |
| cost if the lag were 1 day | ~$14 |
| **saving from shortening retention** | **~$850, once** |
| saving from the compression itself | **~$5,200/year** |

Roughly $850 once, against a $5,200/year win that happens regardless. It is two months of delay,
not a permanent loss.

**Prefix rules do not fit the data.** Lifecycle prefixes are literal object-name prefixes, not
globs — there is no way to express "every `widefield.tar`". The tars sit in **1,119 distinct session
folders across 112 subject folders**, so it would take either 1,119 rules (far beyond B2's per-bucket
rule cap, which is on the order of 100 — verify before relying on it) or 112 subject-level rules.
B2 also rejects overlapping prefixes, so subject-level rules could not coexist with the existing
bucket-wide 60-day rule; it would have to be removed and fully re-expressed. Confirm that
restriction with a single test rule before planning around it either way.

**Subject-level rules would over-apply.** A rule on `Subjects/AL_0033/` covers *everything*
underneath — the SVD outputs, the behavioural files, the videos. Shortening retention there means
that if anything else in those folders is deleted by accident during the campaign, the undo window
is a day instead of two months. That is a bad trade on the only backup.

**If you do want to accelerate it**, the safe version is to temporarily lower the single
bucket-wide rule (60 → say 14 days) during the deletion campaign and restore it afterwards. One
change, uniform, easily reverted. It still thins protection for everything else while it is in
force.

**My recommendation: leave it at 60 days.** During the one operation where we deliberately delete
75 TB, a two-month undo window is worth more than $850. Budget for two months of overlap instead.

## 3. The open question that actually matters: does the deletion even reach B2?

Your findings — no `sudo`, no user `crontab`, nothing in `/etc/cron.d` but `at` and
**`middlewared`**, and no `rclone.conf` anywhere you can see — are consistent and informative.

`middlewared` means **sahale is a TrueNAS box**. On TrueNAS the backup is almost certainly a
**Cloud Sync Task**, which:

- is configured in the web UI, not in cron;
- is executed by `middlewared`, not by a user job;
- **uses rclone underneath**, but keeps its configuration in the TrueNAS config database
  (`/data/freenas-v1.db`), which is why there is no `rclone.conf` in your home or `/etc`.

That is good news for one earlier worry: rclone maps **one object per file**, so this is not a
chunked/deduplicating backup (restic, borg, duplicati) where deleting source files frees nothing
until an expensive prune. Deletions can propagate cleanly.

**But it depends entirely on the task's Transfer Mode:**

| mode | what deleting `widefield.tar` does to B2 |
|---|---|
| **SYNC** | removes it there too; the 60-day clock starts; space is reclaimed |
| **COPY** | **nothing, ever.** The object stays live indefinitely and keeps billing, whatever the lifecycle rule says |
| MOVE | not applicable here |

If the task is set to COPY — a common and deliberate choice, because it protects against exactly
the accident of a source-side deletion — then **none of the 72 TB will ever leave the bill without
someone explicitly deleting the objects in B2.** This is the single most important thing left to
establish, and it is much more consequential than the 60 days.

## 4. How to find out

**Try the TrueNAS web UI first** — you may well have an account even without shell `sudo`. Go to
**Data Protection → Cloud Sync Tasks** and look at the task covering the Subjects dataset:

- **Direction** (Push) and **Transfer Mode** (Sync / Copy / Move) ← the answer
- the **bucket** and **folder** it targets
- any **Exclude** patterns (are `*.tar` or the video files already excluded?)
- whether **Remote encryption** is on (still 1:1 per file, but object names are obfuscated)
- the schedule, and whether it is enabled

**From the shell, without sudo**, these are worth a try and cost nothing:

```bash
cat /etc/version 2>/dev/null; uname -a          # confirms TrueNAS and its release
ls /mnt                                          # TrueNAS pools live here
midclt call cloudsync.query 2>&1 | head -40      # the middleware CLI; may refuse without root
```

`midclt` will probably require root, but it is a one-line thing to try.

## 5. If you do need the admin, ask exactly these

1. Is the B2 backup a TrueNAS Cloud Sync Task, and is its **Transfer Mode SYNC or COPY**?
2. Does it cover the whole `Subjects` dataset, and are there any **exclude patterns** today?
3. Is **Object Lock** enabled on the bucket? (It would block deletion regardless of lifecycle
   rules, and in compliance mode cannot be overridden by anyone.)

Question 1 is the one that decides whether this project reduces the bill at all.

## 6. The empirical check, still the best evidence

Whatever the answers, one controlled test settles it for your exact setup:

1. Pick a session already compressed and verified (`byte_identical: true` in its receipt).
2. Note the bucket size in the B2 UI.
3. Delete that one `widefield.tar`.
4. Check after the next backup run whether the object is *hidden* in B2
   (`b2 ls --versions --recursive b2://<bucket>/<path>/`).

If it is hidden, the mode is SYNC and the space returns in 60 days. If it is still live after a
couple of backup cycles, the mode is COPY and the plan needs a deliberate B2-side deletion step.
Either way you learn it for the price of one file rather than 1,120.

Do this **before** the bulk deletion, while the compressed copy and the SVD outputs both exist.

## 7. Also worth confirming

- **Is `Y:\temp` backed up?** The pilot outputs live there and are disposable.
- **Are the SVD outputs backed up?** ~3.8 TB, and the copy that actually gets analysed — they
  matter more than the raw tars.
- **The overlap window.** The `.wfz` files are new objects, so while originals are still present
  the bucket holds both: ~120.7 TB → ~166 TB → ~48 TB. Excluding `widefield.tar` from the backup
  set once its `.wfz` is verified *and itself backed up* would avoid re-uploading data that is
  about to be deleted — get that ordering right or you open a gap.
