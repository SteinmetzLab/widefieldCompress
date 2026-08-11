# What both campaigns are worth: server space and Backblaze bill

All figures derived from measurements in this repo, not from vendor claims. Reproduce with
`scripts/space_and_cost.py`.

## The two reclamations

### Widefield — 69.8 TB

| | |
|---|---|
| tars as censused | 120.68 TB |
| less the 21 the lab trimmed | −1.23 TB |
| **tars on the share now** | **119.44 TB** |
| ratio achieved so far (212 archives, 41.1 TB) | **×2.41** |
| `.wfz` will occupy | 49.65 TB |
| **reclaimed** | **69.80 TB** |

The ratio is measured, not projected — 212 archives compressed and every one verified
byte-identical.

### Ephys — 58 to 61 TB

| | |
|---|---|
| raw `.bin` on the share | 95.02 TB |
| already has a `.cbin` (raw deletable today) | 0.23 TB |
| still to compress | 94.79 TB |
| at ×2.56 (measured directly) | 37.03 TB kept, **57.99 TB reclaimed** |
| at ×2.82 (implied by the 12 already compressed) | 33.61 TB kept, **61.40 TB reclaimed** |

## The share

`Y:` is 372.79 TB with 115.07 TB free today.

| | used | full |
|---|---|---|
| before any of this | ~241.87 TB | 65% |
| **today** | **257.72 TB** | **69%** |
| after both, conservative | 114.08 TB | **31%** |
| after both, likely | 110.66 TB | **30%** |

**Note the middle row.** Nothing has been deleted yet, so the share is currently *fuller* than when
we started — 17.1 TB of `.wfz` written alongside tars that are all still there. That is the
intended order (write and verify everything, delete nothing until you choose to), but it means the
bill goes up before it goes down.

Net: **~128–131 TB reclaimed, taking the share from 69% full to about 30%.**

## Backblaze B2

At the current pay-as-you-go list price of **$6.95/TB/month**. `Subjects`, `Code` and
`alyx-backup` are backed up; `temp` is not — and everything reclaimed here is under `Subjects`, so
all of it counts.

| | reclaimed | per year |
|---|---|---|
| widefield | 69.80 TB | **$5,821** |
| ephys (conservative) | 57.99 TB | **$4,836** |
| ephys (likely) | 61.40 TB | $5,121 |
| **total** | **~128–131 TB** | **~$10,700 – $10,900 / year** |

That is roughly **$890–910 a month**, indefinitely.

### Transition costs, one-off

- **Both copies coexist until you delete.** Finishing the widefield campaign without deleting
  puts ~49.7 TB of `.wfz` on top of the tars — about **$345/month** extra while that lasts. Add
  the ephys campaign on the same terms and the peak is ~87 TB, about **$600/month**. Running them
  sequentially and deleting between keeps the peak at the lower figure.
- **B2 keeps prior versions for 60 days**, so deleted files keep billing for two more months:
  a one-off of roughly **$1,800** before the savings appear.
- Egress is unaffected — B2 gives free egress up to 3× average monthly storage, far above anything
  this involves.

### Caveats on the price

$6.95/TB/month is B2's list pay-as-you-go rate. If the lab is on **B2 Reserve** (capacity
commitment) or grandfathered at the older $6/TB rate, scale accordingly — at $6.00 the annual
saving is about **$9,200** instead of $10,700. Worth checking the actual invoice before quoting
this to anyone.
