# Step 0: proving the Backblaze undo, before anything relies on it

The deletion plan opens by deleting one small tar and restoring it from Backblaze. That single
test is what turns "B2 keeps prior versions for 60 days" from a setting someone read in a web UI
into a demonstrated recovery path.

**I cannot run it.** Checked on this workstation: no `b2` CLI, no `rclone`, no `rclone.conf` in any
of the three standard locations, no `.b2_account_info`, and no B2 environment variables. `boto3`
exists in the system Python but with no credentials configured. The B2 keys live in the TrueNAS
Cloud Sync Task on sahale, which needs either the server admin or the B2 web console.

That is also why I have not done the delete half. Deleting a file to test whether it can be
restored, when I have no way to restore it, would invert the entire point of the exercise.

## What the test needs to establish

Three separate things, and it is worth being explicit because only the third is the one that
matters:

1. the tar is gone from the server;
2. B2 still holds the object, as a prior version;
3. **the restored bytes hash to the value recorded in the `.wfz`.**

(3) is the test. (2) without (3) only shows that something of the right name came back.

## Suggested subject

Pick from `data/deletable_audit.csv` — any row with `verdict = SAFE`. Use a small one; the point is
the mechanism, not the megabytes. Note its `source_tar_sha256` before doing anything, because that
is what the restored file has to match:

```bash
python -c "import json,sys; print(json.load(open(sys.argv[1]))['source_tar_sha256'])" PATH/widefield.wfz.receipt.json
```

## The procedure

1. **Record the hash and size** of the target tar, from the receipt beside its `.wfz`.
2. **Confirm B2 already holds the `.wfz`.** If the tar goes before its replacement has synced,
   there is a window with one copy of the data on one server. The sync is nightly, so anything
   compressed today has not synced yet.
3. **Delete the tar** on the server.
4. **Wait for the next sync**, so B2 registers the deletion and rolls the object to a prior
   version. Until that happens nothing has actually been tested.
5. **Restore from B2** — web console: browse to the file, enable "show versions", download the
   version from before the delete. Or with the CLI, if the admin provides keys:
   ```bash
   b2 ls --versions b2://BUCKET/Subjects/SUBJ/DATE/N/
   b2 file download b2://BUCKET/Subjects/SUBJ/DATE/N/widefield.tar restored.tar --version-id ID
   ```
6. **Hash the restored file** and compare with step 1:
   ```bash
   sha256sum restored.tar
   ```
7. **Also confirm the `.wfz` route independently**, which needs no B2 at all:
   ```bash
   wfcompress decompress PATH/widefield.wfz restored_from_wfz.tar
   sha256sum restored_from_wfz.tar
   ```
   Both should equal `source_tar_sha256`. If the B2 route fails but this one succeeds, the data is
   still safe and the plan needs a different backstop. If this one fails, stop the whole campaign.

## What each outcome means

| | |
|---|---|
| both hashes match | the plan is sound; proceed to the pilot batch |
| B2 restore fails or hashes differ | **do not delete anything.** The 60-day window is not the safety net we assumed, and the plan needs rethinking — possibly a second on-site copy instead |
| `.wfz` route fails | stop everything and tell me; that would mean a verified archive is not reproducing, which nothing so far suggests is possible |

## What I can do without B2

Condition 8 of the gate — re-hashing the tar on disk and confirming it equals the value the `.wfz`
reconstructs — needs no Backblaze at all, and it is the strongest statement available short of the
restore test. `scripts/audit_deletable.py --strict --rehash-tar` does exactly that. It is a full
read of both files, so it is slow, but it proves for a given archive that the bytes on the server
right now are the bytes the `.wfz` rebuilds.
