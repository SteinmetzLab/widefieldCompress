# Backblaze B2: what to check before deleting anything

**Deleting 75 TB from the share does not reduce the B2 bill.** Not immediately, and depending on
how the bucket is configured, possibly not ever without a settings change. Old versions of deleted
files keep billing as stored data until a lifecycle rule actually removes them.

Three things determine what happens, and they need checking in this order.

---

## 1. What tool writes the backup?

This matters more than any B2 setting, because it decides whether "delete on the source" even
becomes "delete an object in B2".

```bash
ssh <you>@sahale.biostr.washington.edu
systemctl list-timers --all | grep -iE 'backup|b2|rclone|restic|dup'
crontab -l; sudo crontab -l; ls -la /etc/cron.d/
ps aux | grep -iE 'rclone|restic|b2|duplicati|borg|veeam'
ls -la ~/.config/rclone/rclone.conf /etc/rclone.conf 2>/dev/null
```

**File-mirror tools** — `rclone sync`, `b2 sync`, Synology Cloud Sync. One object per file. Deleting
the source deletes (or hides) the object, and §2 governs when the storage is actually released.
This is the good case.

**Chunked/deduplicating tools** — `restic`, `borg`, `duplicati`, `kopia`, Veeam, Arq. Your data is
inside pack files, and there is no object corresponding to `widefield.tar`. Deleting the source
frees **nothing** until you run the tool's own prune/compact, which rewrites packs and, on a
100 TB+ repository, is a slow and I/O-heavy operation you need to plan for.

```bash
# if it turns out to be restic
restic -r <repo> forget --prune --dry-run     # shows what would actually be reclaimed
```

If `rclone` is in use, also check for `--backup-dir`: that moves superseded files to a second
location instead of deleting them, which is a copy the lifecycle rules will not touch.

---

## 2. Bucket lifecycle rules

**Web UI:** Buckets → the bucket → *Lifecycle Settings*.

**CLI:**

```bash
pip install b2
b2 account authorize                  # older versions: b2 authorize-account
b2 bucket get <bucketName>            # older versions: b2 get-bucket <bucketName>
```

Look at `lifecycleRules` in the JSON:

| what you see | what it means |
|---|---|
| `[]` (empty) — "Keep all versions" | **Nothing is ever deleted.** Every version of every file bills forever. This is the default for new buckets and the worst case for you. |
| `daysFromHidingToDeleting: 1` — "Keep only the last version" | A deleted file's last version disappears ~1 day later. Savings land almost immediately. |
| `daysFromHidingToDeleting: N` | Savings land N days after you delete. |
| `daysFromUploadingToHiding: N` | Files are auto-hidden N days after upload regardless of the source — worth understanding if present. |

To see what versions actually exist for a path:

```bash
b2 ls --versions --recursive b2://<bucket>/Subjects/FD_010/2026-02-23/3/
```

A file shows as `upload` (live) or `hide` (a delete marker). Both the marker and the underlying
version persist — and bill — until the lifecycle rule collects them.

---

## 3. Object Lock

If Object Lock is enabled with a retention period, deletion is **blocked** until it expires,
lifecycle rules notwithstanding. In *compliance* mode it cannot be overridden by anyone, including
the account owner.

```bash
b2 bucket get <bucketName> | grep -iE 'fileLock|defaultRetention'
```

Check this before planning any timeline. It is the one setting that can make "reclaim the space"
impossible on your preferred schedule.

---

## The overlap window — the bill goes up first

The compressed files are new objects, so they get backed up too. While you keep originals (and you
should, at least for the first tranche), the bucket holds both.

At B2's list price of roughly **$6/TB/month** — check your actual invoice, rates and any negotiated
discount vary:

| stage | on B2 | ≈ /month |
|---|---|---|
| today | 120.7 TB | ~$725 |
| both originals and `.wfz` | ~166 TB | ~$995 |
| after versions expire | ~45 TB | ~$270 |

So roughly **$5,500/year** saved once it settles, but a few months of *higher* spend on the way
there. Two ways to shorten that window:

- **Exclude `widefield.tar` from the backup set** once the matching `.wfz` is verified *and* itself
  backed up. Stops re-uploading data you are about to delete. Get the ordering right — the `.wfz`
  must be safely in B2 before the tar leaves the backup set, or you have a gap.
- **Delete in tranches** rather than all at once, so the overlap applies to a slice at a time.

As far as I know B2 has no minimum storage-duration charge (unlike S3 IA/Glacier), so deletion stops
the meter as soon as the version is actually removed — but confirm that against an invoice rather
than taking my word for it.

---

## The empirical check, which beats reading any of the above

Your setup is what it is, and one controlled test answers the question definitively:

1. Pick one session that is already compressed and verified (`byte_identical: true` in its receipt).
2. Note the bucket's current size: `b2 get-bucket <bucket>` or the *Buckets* page.
3. Delete that one `widefield.tar`.
4. Watch the bucket size daily for a couple of weeks.

If it drops by ~100 GB after N days, you know the lifecycle delay and that the mapping is 1:1. If it
never drops, you have either "keep all versions", a chunked backup tool awaiting a prune, or Object
Lock — and you have learned that for the price of one file rather than 1,100.

Do this **before** the bulk delete, and while the compressed copy plus the original SVD outputs both
still exist.

---

## Also worth confirming

- **Is `Y:\temp` backed up?** The Phase 0.5 and pilot outputs live there. If it is, they are being
  billed, and they are disposable.
- **Are the SVD outputs backed up?** They are ~3.8 TB and are the copy that actually gets analysed.
  They matter more than the raw tars.
- **What is the restore path?** If 45 TB ever has to come back, egress is free up to 3× stored data
  per month under B2's current terms, but the wall-clock time is the real constraint. Worth knowing
  the number before you need it.
