# Giving this workstation read-only Backblaze access

Read-only is the right scope. It closes the one gate condition that cannot be checked from here —
*is the `.wfz` actually in the offsite backup before its tar is deleted?* — and it makes it
structurally impossible for this tooling to remove anything from B2. The undo stays entirely in
your hands.

## Please do not send me the key

Not in chat, not in a file, not in a commit. I will not type an application key into anything.
The pattern that works, and the one the Netlify setup on this machine already uses, is: **you
authorize once in your own terminal, and I use the resulting cached session.** The CLI stores a
token in `~/.b2_account_info`; I never need to see the key that produced it.

If the key is ever pasted into this conversation, treat it as compromised and rotate it.

## 1. Create the key (B2 web console)

**Account → Application Keys → Add a New Application Key**

| field | value |
|---|---|
| Name | `wfcompress-readonly` |
| Allow access to Bucket | the single bucket holding the sahale backup, not "All" |
| Type of Access | **Read Only** |
| File name prefix | `Subjects/` — optional, but there is no reason for this key to see anything else |
| Duration | optional; a 90-day expiry is reasonable and costs nothing |

Read Only grants `listBuckets`, `listFiles`, `readFiles`, `readBucket*`. It cannot write, delete,
or hide. Note that a key scoped to one bucket **cannot** call `listBuckets`, so `b2 bucket list`
will fail with that key — that is expected, not a misconfiguration. Just tell me the bucket name;
it is not a secret.

## 2. Authorize, in your own terminal

The CLI is already installed at `D:\temp\wfc-venv\Scripts\b2.exe` (v4.7.1). Run this yourself:

```bash
D:\temp\wfc-venv\Scripts\b2.exe account authorize
```

With no arguments it prompts for the key ID and key interactively, so neither ends up in your
shell history. Passing them on the command line also works but leaves them in history — prefer
the prompt.

## 3. Tell me the bucket name

That is all I need. I will confirm with `b2 account get`, which prints the account and
capabilities but not the key.

## What I will use it for

Only these, all read-only:

| | |
|---|---|
| `b2 file info` | does this `.wfz` exist in B2, and is it the right size? — **gate condition 7** |
| `b2 ls --versions` | does B2 still hold the pre-delete version of a tar? |
| `b2 file download` | pull a restored copy back so its SHA-256 can be compared |

`scripts/check_b2_presence.py` (written, waiting on auth) does the first of these across every
archive the deletion audit marked SAFE, and reports present / missing / wrong-size.

## What it still will not let me do

- Delete anything in B2. Good.
- Delete anything on the server — that is a filesystem operation, and there is still no delete
  path anywhere in `wfcompress`. When we get to step 0, the one tar deletion is something I would
  do only on an explicit go-ahead, for a named archive, after the B2 copy has been confirmed
  present *and* downloaded and hash-checked first.

That ordering matters: **confirm the restore works before removing the original**, not after. It
inverts the usual "delete then test" framing of step 0, and it is strictly safer — the only cost
is downloading one file twice.
