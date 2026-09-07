"""Count distinct items per bib_fmt, by OCLC and by a derived title+author key.

Two caveats the numbers depend on:
  * oclc_num is multi-valued in ~15.7% of rows ("217097625,252"), so the raw field
    and its first value are counted separately.
  * OCLC identifies a work/edition, not a physical volume - several htids legitimately
    share one OCLC. That is what makes it useful for collapsing duplicate scans.

64-bit hashes are stored instead of the strings themselves to keep memory flat.
"""
import csv, gzip, re, sys
from collections import defaultdict

sys.path.insert(0, "scripts")
from compare_overlap import norm_full

HATHI = "data/experiment/hathi_full_20260801.txt.gz"
PD = {"pd", "pdus", "pd-pvt"}
FMT_NAME = {"BK": "Book", "SE": "Serial", "MU": "Music/score", "MP": "Map",
            "MX": "Mixed materials", "VM": "Visual material", "CF": "Computer file",
            "XX": "unknown", "": "(blank)"}


def surname(name):
    return name.split(",")[0].strip().lower() if name else ""


rows = defaultdict(int)
htids = defaultdict(set)
oclc_raw = defaultdict(set)
oclc_first = defaultdict(set)
ta_key = defaultdict(set)
no_oclc = defaultdict(int)
no_ta = defaultdict(int)

pd_only = "--pd" in sys.argv
n = 0
with gzip.open(HATHI, "rt", encoding="utf-8", errors="replace") as fh:
    for f in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(f) < 26:
            continue
        if pd_only and f[2] not in PD:
            continue
        n += 1
        fmt = f[19]
        rows[fmt] += 1
        htids[fmt].add(hash(f[0]))

        o = f[7].strip()
        if o:
            oclc_raw[fmt].add(hash(o))
            oclc_first[fmt].add(hash(o.split(",")[0].strip()))
        else:
            no_oclc[fmt] += 1

        t, a = norm_full(f[11]), surname(f[25])
        if t:
            ta_key[fmt].add(hash(t + "|" + a))
        else:
            no_ta[fmt] += 1

        if n % 8000000 == 0:
            print(f"  ...{n:,} rows", flush=True)

scope = "PUBLIC DOMAIN ONLY" if pd_only else "ALL ROWS"
print(f"\n================ {scope} ================")
hdr = f"{'bib_fmt':<8} {'rows':>12} {'distinct htid':>14} {'distinct OCLC':>14} {'OCLC 1st':>12} {'title+author':>13}"
print(hdr)
print("-" * len(hdr))
for fmt in sorted(rows, key=lambda k: -rows[k]):
    print(f"{fmt or '(blank)':<8} {rows[fmt]:>12,} {len(htids[fmt]):>14,} "
          f"{len(oclc_raw[fmt]):>14,} {len(oclc_first[fmt]):>12,} {len(ta_key[fmt]):>13,}")
tot = sum(rows.values())
print("-" * len(hdr))
print(f"{'TOTAL':<8} {tot:>12,} {sum(len(v) for v in htids.values()):>14,} "
      f"{sum(len(v) for v in oclc_raw.values()):>14,} {sum(len(v) for v in oclc_first.values()):>12,} "
      f"{sum(len(v) for v in ta_key.values()):>13,}")

print(f"\nCollapse ratio (rows per distinct key), main formats:")
for fmt in ("BK", "SE", "MU"):
    if rows.get(fmt):
        print(f"  {fmt:<4} {FMT_NAME[fmt]:<14} OCLC {rows[fmt]/max(1,len(oclc_first[fmt])):>5.2f}x   "
              f"title+author {rows[fmt]/max(1,len(ta_key[fmt])):>5.2f}x")
print(f"\nrows with no OCLC : {sum(no_oclc.values()):,}")
print(f"rows with no title: {sum(no_ta.values()):,}")
