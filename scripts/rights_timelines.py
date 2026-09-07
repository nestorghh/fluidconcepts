"""Build per-work rights determination timelines.

IMPORTANT about what this is: a hathifile row carries one rights value and one
rights_timestamp - the *current* determination for that volume. There is no history
column. What this reconstructs is the sequence of determinations made across the
different copies of the same work (title + author), ordered by when each was decided.

So a timeline reading "ic 2006 -> pd 2013 -> pd 2022" means HathiTrust judged three
different physical copies of that work on those dates, reaching those conclusions -
not that one volume's status was revised. That pattern is still the interesting one:
it is where the library's assessment of a work visibly diverges or shifts over time.

Two passes so memory stays bounded.
"""
import csv, gzip, json, sys
from collections import defaultdict

import pandas as pd

sys.path.insert(0, "scripts")
from compare_overlap import norm_full
from dedup_hathitrust import norm_author

HATHI = "data/experiment/hathi_full_20260801.txt.gz"
OUT = "demo/output/rights_timelines"
PD = {"pd", "pdus", "pd-pvt"}
TOP_N = 400

RIGHTS_CODES = {}


def code(r):
    return RIGHTS_CODES.setdefault(r, len(RIGHTS_CODES))


def rows(books_only=True):
    with gzip.open(HATHI, "rt", encoding="utf-8", errors="replace") as fh:
        for f in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
            if len(f) >= 26 and (not books_only or f[19] == "BK"):
                yield f


def main():
    # pass 1: which works have copies carrying more than one distinct rights value?
    mask = {}
    n = 0
    print("pass 1: finding works with divergent rights determinations ...")
    for f in rows():
        n += 1
        k = hash(norm_full(f[11]) + "|" + norm_author(f[25]))
        b = 1 << code(f[2])
        mask[k] = mask.get(k, 0) | b
        if n % 4_000_000 == 0:
            print(f"  ...{n:,} book rows, {len(mask):,} works", flush=True)

    interesting = {k for k, v in mask.items() if bin(v).count("1") >= 2}
    print(f"\n  book rows            : {n:,}")
    print(f"  distinct works       : {len(mask):,}")
    print(f"  works with >=2 rights: {len(interesting):,} "
          f"({100*len(interesting)/len(mask):.2f}%)")
    del mask

    # pass 2: collect every copy of those works
    print("\npass 2: collecting copies of those works ...")
    groups = defaultdict(list)
    for f in rows():
        k = hash(norm_full(f[11]) + "|" + norm_author(f[25]))
        if k in interesting:
            groups[k].append({
                "htid": f[0], "rights": f[2], "access": f[1], "reason": f[13],
                "ts": f[14], "year": f[16], "imprint": f[12],
                "title": f[11], "author": f[25], "lang": f[18],
            })
    print(f"  collected {sum(len(v) for v in groups.values()):,} copies "
          f"across {len(groups):,} works")

    # rank: most distinct rights, then most transitions, then longest span
    works = []
    for k, copies in groups.items():
        copies.sort(key=lambda c: c["ts"])
        seq = [c["rights"] for c in copies]
        transitions = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
        distinct = len(set(seq))
        years = [c["ts"][:4] for c in copies if c["ts"][:4].isdigit()]
        span = (int(max(years)) - int(min(years))) if years else 0
        flips_to_pd = sum(1 for a, b in zip(seq, seq[1:]) if a not in PD and b in PD)
        works.append({
            "title": copies[0]["title"], "author": copies[0]["author"],
            "n_copies": len(copies), "n_distinct_rights": distinct,
            "n_transitions": transitions, "span_years": span,
            "flips_to_pd": flips_to_pd,
            "first_rights": seq[0], "last_rights": seq[-1],
            "first_ts": copies[0]["ts"], "last_ts": copies[-1]["ts"],
            "events": copies,
        })

    # Persist every event so the dashboard selection can be re-sliced without rerunning.
    ev = pd.DataFrame([dict(e, work_title=w["title"]) for w in works for e in w["events"]])
    ev.to_parquet(f"{OUT}_events.parquet", index=False)
    print(f"  wrote {OUT}_events.parquet ({len(ev):,} events)")

    # For display, prefer stories a reader can follow: several distinct rights values
    # and a real time span, but few enough copies to plot legibly. Thousand-volume
    # sets dominate a raw transition count and render as noise.
    def score(w):
        legible = 3 <= w["n_copies"] <= 40
        return (legible, w["n_distinct_rights"], w["flips_to_pd"],
                w["span_years"], w["n_transitions"])
    works.sort(key=score, reverse=True)

    df = pd.DataFrame([{k: v for k, v in w.items() if k != "events"} for w in works])
    df.to_parquet(f"{OUT}_summary.parquet", index=False)
    df.to_csv(f"{OUT}_summary.csv", index=False)
    with open(f"{OUT}.json", "w") as fh:
        json.dump(works[:TOP_N], fh, default=str)

    print(f"\nwrote {OUT}_summary.parquet / .csv  ({len(df):,} works)")
    print(f"wrote {OUT}.json  (top {TOP_N} for the dashboard)")

    print(f"\n  works that ever flip INTO public domain : "
          f"{(df.flips_to_pd > 0).sum():,}")
    print(f"  works with 3+ distinct rights values    : "
          f"{(df.n_distinct_rights >= 3).sum():,}")
    print(f"  max transitions seen                    : {df.n_transitions.max()}")
    print("\nTOP 10 BY TRANSITIONS")
    pd.set_option("display.width", 190, "display.max_colwidth", 52)
    print(df.head(10)[["title", "n_copies", "n_distinct_rights", "n_transitions",
                       "span_years", "first_rights", "last_rights"]].to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
