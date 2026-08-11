"""Server size and Backblaze B2 cost, before and after both reclamations.

Everything here is derived from measurements in this repo rather than assumed:
  widefield  data/census_Y.csv, corrected for the 21 archives the lab trimmed, and the ratio
             actually achieved so far in data/bulk.jsonl
  ephys      data/ephys_census_Y.csv and the mtscomp ratios measured in docs/EPHYS_COMPRESSION.md
"""
import csv
import json
from pathlib import Path

B2_PER_TB_MONTH = 6.95   # backblaze.com/cloud-storage/pricing, pay-as-you-go
SHARE_USED_TB = 257.72   # Get-PSDrive Y, measured today
SHARE_TOTAL_TB = 372.79

# ---- widefield ---------------------------------------------------------------------------
cen = list(csv.DictReader(Path("data/census_Y.csv").open(encoding="utf-8")))
wf = [r for r in cen if r["kind"] in ("frame-N", "basler-tiff") and int(r["bytes"]) > 0]
census_tb = sum(int(r["bytes"]) for r in wf) / 1e12

# the 21 mixed archives shrank after the census was taken
det = list(csv.DictReader(Path("data/mixed_archives_detail.csv").open(encoding="utf-8")))
trimmed_from = sum(int(r["bytes"]) for r in det) / 1e12
trimmed_to = 2.13   # measured after the lab's trim
wf_now_tb = census_tb - trimmed_from + trimmed_to

rows = [json.loads(l) for l in Path("data/bulk.jsonl").read_text().splitlines() if l.strip()]
ok = [r for r in rows if r.get("ok")]
done_src = sum(r["source_bytes"] for r in ok) / 1e12
done_out = sum(r["output_bytes"] for r in ok) / 1e12
wf_ratio = done_src / done_out

wf_after = wf_now_tb / wf_ratio
wf_saved = wf_now_tb - wf_after

# ---- ephys -------------------------------------------------------------------------------
eph = list(csv.DictReader(Path("data/ephys_census_Y.csv").open(encoding="utf-8")))
eph_tb = sum(int(r["bytes"]) for r in eph) / 1e12
eph_done = [r for r in eph if r["has_cbin"] == "True"]
eph_todo_tb = sum(int(r["bytes"]) for r in eph if r["has_cbin"] != "True") / 1e12
eph_banked = sum(int(r["bytes"]) for r in eph_done) / 1e12

print("=" * 78)
print("WIDEFIELD")
print(f"  tars, as censused          {census_tb:7.2f} TB")
print(f"  less the lab's trim of 21  {trimmed_to - trimmed_from:+7.2f} TB")
print(f"  tars on the share now      {wf_now_tb:7.2f} TB")
print(f"  measured ratio so far      x{wf_ratio:.2f}  ({len(ok)} archives, {done_src:.1f} TB)")
print(f"  -> .wfz will occupy        {wf_after:7.2f} TB")
print(f"  -> RECLAIMED               {wf_saved:7.2f} TB")

print("\nEPHYS")
print(f"  raw .bin on the share      {eph_tb:7.2f} TB")
print(f"  of which already .cbin'd   {eph_banked:7.2f} TB (raw deletable now)")
print(f"  still to compress          {eph_todo_tb:7.2f} TB")
for label, ratio in (("measured x2.56", 2.56), ("observed x2.82", 2.82)):
    after = eph_todo_tb / ratio
    print(f"  at {label}: -> {after:6.2f} TB kept, RECLAIMED "
          f"{eph_todo_tb - after + eph_banked:6.2f} TB")

print("\n" + "=" * 78)
print("SERVER TOTAL (Y:, 372.79 TB capacity)")
wfz_written = done_out
before = SHARE_USED_TB - wfz_written + (trimmed_from - trimmed_to)
print(f"  before any of this started ~{before:7.2f} TB used   "
      f"({100*before/SHARE_TOTAL_TB:.0f}% full)")
print(f"  today                       {SHARE_USED_TB:7.2f} TB used   "
      f"({100*SHARE_USED_TB/SHARE_TOTAL_TB:.0f}% full)   "
      f"[+{wfz_written:.1f} TB of .wfz written, tars not yet deleted]")
for label, eratio in (("conservative x2.56", 2.56), ("likely x2.82", 2.82)):
    e_saved = eph_todo_tb - eph_todo_tb / eratio + eph_banked
    after_all = before - wf_saved - e_saved
    print(f"  after both, {label:<19} {after_all:7.2f} TB used   "
          f"({100*after_all/SHARE_TOTAL_TB:.0f}% full)   "
          f"saving {wf_saved + e_saved:.1f} TB")

print("\n" + "=" * 78)
print(f"BACKBLAZE B2 at ${B2_PER_TB_MONTH:.2f}/TB/month")
print("  (Subjects, Code and alyx-backup are backed up; temp is not)")
for label, eratio in (("conservative x2.56", 2.56), ("likely x2.82", 2.82)):
    e_saved = eph_todo_tb - eph_todo_tb / eratio + eph_banked
    tot = wf_saved + e_saved
    print(f"\n  {label}:  {tot:.1f} TB less stored")
    print(f"    widefield  {wf_saved:6.2f} TB  ->  ${wf_saved*B2_PER_TB_MONTH*12:8,.0f} / year")
    print(f"    ephys      {e_saved:6.2f} TB  ->  ${e_saved*B2_PER_TB_MONTH*12:8,.0f} / year")
    print(f"    TOTAL      {tot:6.2f} TB  ->  ${tot*B2_PER_TB_MONTH*12:8,.0f} / year "
          f"(${tot*B2_PER_TB_MONTH:,.0f}/month)")

print("\n  transition costs, one-off:")
peak = wf_after + eph_todo_tb / 2.56
print(f"    both copies coexist while each campaign runs: up to "
      f"+{wf_after + eph_todo_tb/2.56:.0f} TB at peak")
print(f"      = ${peak*B2_PER_TB_MONTH:,.0f}/month extra while that lasts")
print(f"    B2 keeps prior versions 60 days, so deletions keep billing for 2 months:")
print(f"      ~${(wf_saved + eph_todo_tb - eph_todo_tb/2.56)*B2_PER_TB_MONTH*2:,.0f} total")
