"""For each (title, author), keep the row with the newest rights_timestamp.

All 26 columns of the winning row are preserved, plus n_copies.

Two streaming passes so memory stays bounded at ~19.6M rows:
  pass 1  decide the winning line number for every key
  pass 2  re-read and emit only those lines

Rather than hold a tuple per key, the winner is packed into one integer
(timestamp, line number, copy count), which roughly halves the dict overhead.
"""
import argparse
import csv, gzip, re, sys
import pandas as pd

sys.path.insert(0, "scripts")
from compare_overlap import norm_full
from dedup_hathitrust import norm_author

HATHI = "data/experiment/hathi_full_20260801.txt.gz"
OUT = "data/experiment/hathi_latest_by_title_author"
COLS = ["htid","access","rights","ht_bib_key","description","source","source_bib_num",
        "oclc_num","isbn","issn","lccn","title","imprint","rights_reason_code",
        "rights_timestamp","us_gov_doc_flag","rights_date_used","pub_place","lang",
        "bib_fmt","collection_code","content_provider_code","responsible_entity_code",
        "digitization_agent_code","access_profile_code","author"]

CNT_BITS, LINE_BITS = 20, 25          # count < 1,048,576, line no < 33.5M
CNT_MASK, LINE_MASK = (1 << CNT_BITS) - 1, (1 << LINE_BITS) - 1
DIGITS = re.compile(r"\D")


def ts_int(s):
    """'2022-12-31 19:07:31' -> 20221231190731, so comparison is a plain int compare."""
    d = DIGITS.sub("", s or "")
    return int(d[:14].ljust(14, "0")) if d else 0


def key_of(f):
    return hash(norm_full(f[11]) + "|" + norm_author(f[25]))


def rows(path, bib_fmt=None):
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        for f in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
            if len(f) >= 26 and (bib_fmt is None or f[19] == bib_fmt):
                yield f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bib-fmt", default=None, help='restrict to one format, e.g. BK')
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    out_base, fmt = args.out, args.bib_fmt
    print(f"scope: {'bib_fmt=' + fmt if fmt else 'all formats'}\n")

    best = {}

    print("pass 1: finding the newest row per (title, author) ...")
    for i, f in enumerate(rows(HATHI, fmt)):
        k = key_of(f)
        t = ts_int(f[14])
        prev = best.get(k)
        if prev is None:
            best[k] = (t << (LINE_BITS + CNT_BITS)) | (i << CNT_BITS) | 1
        else:
            cnt = min((prev & CNT_MASK) + 1, CNT_MASK)
            if t > (prev >> (LINE_BITS + CNT_BITS)):
                best[k] = (t << (LINE_BITS + CNT_BITS)) | (i << CNT_BITS) | cnt
            else:
                best[k] = (prev & ~CNT_MASK) | cnt
        if (i + 1) % 5_000_000 == 0:
            print(f"  ...{i+1:,} rows, {len(best):,} keys", flush=True)

    total = i + 1
    print(f"\n  rows scanned : {total:,}")
    print(f"  distinct keys: {len(best):,}")
    print(f"  collapse     : {total/len(best):.2f}x")

    winners = {(v >> CNT_BITS) & LINE_MASK: v & CNT_MASK for v in best.values()}
    del best

    print("\npass 2: writing the winning rows ...")
    out = []
    for i, f in enumerate(rows(HATHI, fmt)):
        n = winners.get(i)
        if n is not None:
            out.append(f[:26] + [n])
        if (i + 1) % 5_000_000 == 0:
            print(f"  ...{i+1:,} rows, {len(out):,} kept", flush=True)

    df = pd.DataFrame(out, columns=COLS + ["n_copies"])
    df["n_copies"] = df["n_copies"].astype(int)
    df = df.sort_values(["n_copies", "rights_timestamp"], ascending=False).reset_index(drop=True)

    df.to_parquet(f"{out_base}.parquet", index=False)
    print(f"\nwrote {out_base}.parquet   ({len(df):,} rows x {len(df.columns)} cols)")
    df.to_csv(f"{out_base}.csv", index=False)
    print(f"wrote {out_base}.csv")

    df.head(10).to_csv(f"{out_base}_top10.csv", index=False)
    print(f"wrote {out_base}_top10.csv")

    pd.set_option("display.width", 200, "display.max_colwidth", 44)
    print(f"\nTOP 10 BY n_copies\n{'='*150}")
    print(df.head(10)[["title","author","rights","rights_timestamp","rights_date_used",
                       "bib_fmt","n_copies"]].to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
