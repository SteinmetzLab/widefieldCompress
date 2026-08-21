# Why the widefield campaign keeps stopping, and what it has cost

Written 2026-08-17. Read-only investigation plus one action: the campaign was restarted.

## The headline

Between 2026-08-13 and 2026-08-17 the widefield campaign was **stopped for about 90 of 118
hours — 76% of the elapsed time**. Compression is not slow. It is idle.

The compressor itself is healthy: 326 of 1,120 archives, 58.18 TB in, 24.20 TB out, x2.40,
**326/326 byte-identical**. Not one verification failure in the whole campaign.

## Timeline, reconstructed from logs

All times PDT. Sources: `data/fileEditLog.csv`, `D:\temp\wfc_supervisor.log`,
`~/ephys_files.csv` on sahale, `kern.boottime`, `last reboot`, Windows event log.

| when | what |
|---|---|
| 08-13 09:06 | previous driver dies, exit `0xC000013A` (console window closed) |
| 08-13 09:07 | supervisor relaunches; reclaims 6 stale partials |
| 08-13 09:49-10:24 | driver opens 16 `.partial-*` files, i.e. 8 archives in flight |
| **08-13 11:05** | **ephys campaign launched on sahale, 8 procs x 4 threads** |
| 08-13 11:05 | ephys opens its own 8 `.cbin.partial-*` |
| **08-13 11:39** | **every one of those 16 files stops growing. Both campaigns stall.** |
| 08-13 11:39-17:44 | sahale wedged: load average 35, no SSH/SMB/HTTP response |
| **08-13 17:44** | **hard reboot by the admin. No `shutdown` record — unclean.** |
| 08-14 08:44 | widefield restarted alone; reclaims 16 stale partials |
| 08-14 08:44-21:07 | runs cleanly for 12.4 h, 24 archives, all byte-identical |
| **08-14 21:07** | **driver and supervisor both vanish, mid-flight, no log line** |
| 08-15 - 08-17 | nothing running. B2 drains the entire backlog. |
| 08-17 17:04 | restarted; reclaimed 6 stale partials, 130.3 GB |

### The 08-13 stall was caused by running both campaigns

This is now measured rather than inferred. The two campaigns opened 16 output files between
09:49 and 11:05, and **all sixteen have the same last-modified time, 11:39** — 34 minutes after
the ephys job started. Independent jobs on two different machines do not stop within the same
minute by coincidence. The shared resource is sahale's pool, and it went down with them.

Cost: 8 widefield archives and 8 ephys files thrown away, plus a 22-hour outage of the lab's
file server.

This confirms the existing rule: **one campaign at a time.** Do not start ephys while widefield
is running.

### The 08-14 death is still unexplained

Different failure, and the more expensive one, because it went unnoticed for 2.8 days.

What is ruled out:

- **Not a workstation reboot.** Last boot 2026-07-16; uptime is continuous across the event.
- **Not sahale.** It booted 08-13 17:44 and has been up ever since; it was healthy at 21:07.
- **Not a crash.** Nothing in the Windows Application or System log between 20:00 and 23:00
  except an unrelated DCOM warning and a WhatsApp update failure. `wfc_run_001.err` was empty.
- **Not a clean exit.** The supervisor's whole purpose is to log the exit code and relaunch. It
  logged nothing, so **the supervisor died at the same instant as the driver.**
- **Not the console-close bug** fixed on 08-13: the supervisor already spawns `pythonw.exe`,
  which has no console.

A parent and child dying together with no exit code, no error and no reboot is the signature of
an external `TerminateProcess` on a **process tree** — a Windows job object being closed. The
leading suspect remains the one named in `HANDOFF.md`: the agent session that launched the
supervisor exits, and Windows tears down its job. The last write to that session's transcript
was 08-14 13:50, 7.3 h before the death, which fits a terminal window left open and then closed.

**This is a hypothesis, not a measurement.** It is consistent with all four silent deaths so far
and inconsistent with nothing, but it has not been reproduced deliberately.

## The fix that has not been applied

Register the supervisor as a **Scheduled Task**, so it runs under the Task Scheduler service and
is owned by no interactive session or agent process. This is persistent machine configuration,
so it needs Nick's say-so; it has not been done.

A per-user task needs no administrator rights, roughly:

```powershell
$a = New-ScheduledTaskAction -Execute 'D:\temp\wfc-venv\Scripts\pythonw.exe' `
       -Argument 'scripts\supervise_bulk.py' -WorkingDirectory 'D:\Dropbox\code\widefieldCompress'
$t = New-ScheduledTaskTrigger -AtLogOn
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
       -RestartInterval (New-TimeSpan -Minutes 5) -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'wfcompress-supervisor' -Action $a -Trigger $t -Settings $s
```

`-ExecutionTimeLimit 0` matters: the default kills the task after 72 h, which is shorter than the
job. The stop file (`D:\temp\wfc_stop`) still works as the clean way to halt it, and the task
should be unregistered when the campaign finishes.

Second, smaller gap: **nothing notices when the campaign stops.** 2.8 days passed before anyone
looked. `status.py` reports it correctly but has to be run by hand.

## Leftovers from the 08-13 crash

**121.4 GB of stale `.cbin.partial-*` on sahale**, eight files, frozen at 08-13 11:39:

```
FD_013/2026-07-30/1        12.5 GB      JRS_0057/2026-06-04/1                13.8 GB
FD_012/2026-05-05/1        15.2 GB      JRS_0057/.../whole_train_...         13.7 GB
FD_012/2026-05-06/1        13.6 GB      JRS_0059/2026-02-13                  15.6 GB
FD_013/2026-07-29/4        13.0 GB      JRS_0022/2023-12-04/1                24.0 GB
```

They are inert — nothing reads a `.partial-*`, and the source `.bin` files are untouched — and
`ephys_compress.py`'s `clean_partials()` removes them automatically on its next run. But if ephys
is not restarted for weeks, that is 121 GB held for nothing. Deleting them by hand is safe.

Note the fourth entry is inside `kilosort4/whole_train_artifact_removed_full/`, the known 347.5 GB
derived duplicate. Worth excluding from the ephys corpus rather than compressing it.
