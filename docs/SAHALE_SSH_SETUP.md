# SSH access to sahale for the ephys campaign

## First, a correction

I argued against this on the grounds that an SSH session would give write access to the whole
Subjects tree, unlike the read-only B2 key. **That was wrong.** `Y:` is a mapped SMB share to
sahale's `data` pool, mounted with the user's credentials, and this workstation has had read/write
access to every byte under it since the first day of the project — every `.wfz`, every sidecar and
`Y:/temp/pylibs` were written through it. The destructive capability was already there.

What SSH actually adds is narrower than I implied:

| | already available via `Y:` | added by SSH |
|---|---|---|
| read/write the Subjects tree | **yes** | — |
| delete the corpus | **yes** | — |
| paths outside the `data` dataset | no | yes (home directory, other `/mnt` datasets) |
| **run processes on the appliance** | no | **yes** |

Only the last row is a genuinely new class of risk, and it is one the plan already commits to: the
ephys campaign is going to run on that box either way. The question is only whether it is driven by
hand or not.

## Why it is worth having

The ephys run is roughly eight days of work across ~1,900 files. It needs the same things the
widefield campaign needed: monitoring, log inspection, and restarting after failures. The widefield
driver has now died silently **three times**, which is why it runs under a supervisor. Driving the
equivalent by copy-paste, for a week, is not a serious plan.

## Setup

A dedicated key has been generated on the workstation — not the user's own key, so it can be
revoked on its own by deleting one line:

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBDHSh3AIWixkY0TOkMn8qM7894U9oEXHLpEfzCpU+2z wfcompress-automation@DESKTOP-KH3D2OM
```

Install it on sahale, from your own session:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBDHSh3AIWixkY0TOkMn8qM7894U9oEXHLpEfzCpU+2z wfcompress-automation@DESKTOP-KH3D2OM' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

The private half stays in `C:\Users\nicks\.ssh\sahale_wfc` and is never transmitted anywhere. It
has no passphrase, for the same reason the B2 token and the Netlify login on this machine do not:
an unattended campaign cannot stop to be unlocked. It is protected by the same filesystem
permissions as those.

### Optional hardening, worth one line

Restrict the key to this workstation by prefixing the `authorized_keys` entry:

```
from="172.25.53.3" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBDHSh3AIWixkY0TOkMn8qM7894U9oEXHLpEfzCpU+2z wfcompress-automation@DESKTOP-KH3D2OM
```

That is this machine's address as sahale sees it. If it is on DHCP the entry will need updating
when the lease changes; if that is a nuisance, leave it off.

## How it will be used

- launch and supervise `ephys_compress.py`;
- read its logs and the run's JSONL;
- inspect files, sizes and free space.

Not for: anything under `/etc` or another user's data, and no deletion of raw data. That last one
is a commitment rather than a technical control — but it is the same commitment that has held for
the widefield corpus over several weeks with full write access to it, and `wfcompress` still has no
delete path anywhere in it.

## Revoking

Delete the line from `~/.ssh/authorized_keys`. Nothing else on the box changes, and the workstation
loses shell access immediately while keeping the SMB share it has always had.
