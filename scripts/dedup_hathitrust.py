"""Deduplicate the HathiTrust rows on a normalized (title, author) key.

Rows are volumes: the same work scanned by many libraries appears many times. This
collapses them to one row per work, keeping the first copy seen plus a n_copies
count and the list of contributing libraries.

Streams the Parquet in batches so memory stays flat over 19.6M rows.
"""
import argparse
import re, sys
import pandas as pd
import pyarrow.dataset as ds

sys.path.insert(0, "scripts")
from compare_overlap import norm_full

COMBINED = "data/experiment/combined.parquet"
DATES = re.compile(r"\d{4}")
PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
WS = re.compile(r"\s+")


def norm_author(a):
    """'Carroll, Lewis, 1832-1898.' -> 'carroll lewis' so date variants merge."""
    # Missing values arrive from Arrow as NaN floats, which are truthy - guard on type.
    if not isinstance(a, str) or not a:
        return ""
    a = DATES.sub(" ", a.lower())
    a = PUNCT.sub(" ", a)
    return WS.sub(" ", a).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("author", nargs="*", default=["carroll", "lewis"])
    ap.add_argument("--pd", action="store_true", help="public domain rows only")
    ap.add_argument("--books", action="store_true", help="bib_fmt=BK only")
    ap.add_argument("--out", default="carroll_dedup")
    args = ap.parse_args()
    who = " ".join(args.author) or "carroll lewis"
    terms = who.lower().split()

    # Filters are applied to ROWS before the key is built, so a work is represented
    # by its qualifying copies only - n_copies counts public-domain scans, not all.
    expr = ds.field("source") == "hathitrust"
    if args.pd:
        expr = expr & (ds.field("is_public_domain") == True)   # noqa: E712
    if args.books:
        expr = expr & (ds.field("bib_fmt") == "BK")
    scope = ("public-domain " if args.pd else "") + ("books" if args.books else "rows")
    print(f"scope: hathitrust {scope}")

    dataset = ds.dataset(COMBINED, format="parquet")
    scanner = dataset.scanner(
        columns=["id", "title", "author", "rights", "bib_fmt", "language",
                 "publication_year", "publisher", "oclc_num", "source_url"],
        filter=expr,
        batch_size=250_000,
    )

    seen_global = set()
    rows_total = 0
    hits = []

    for batch in scanner.to_batches():
        df = batch.to_pandas()
        rows_total += len(df)
        t = df["title"].map(lambda x: norm_full(x) if isinstance(x, str) else "")
        a = df["author"].map(norm_author)
        df["_key"] = t + "|" + a
        seen_global.update(df["_key"].values)

        # author must contain every search term (order-independent)
        mask = a.apply(lambda s: all(term in s for term in terms))
        if mask.any():
            hits.append(df[mask].copy())
        if rows_total % 5_000_000 < 250_000:
            print(f"  ...{rows_total:,} rows", flush=True)

    print(f"\nHathiTrust rows in scope   : {rows_total:,}")
    print(f"distinct (title, author)   : {len(seen_global):,}")
    print(f"collapse ratio             : {rows_total/len(seen_global):.2f}x")

    if not hits:
        print(f"\nno rows matched author terms {terms}")
        return 0

    m = pd.concat(hits, ignore_index=True)
    print(f"\nrows for '{who}'           : {len(m):,}")

    agg = (m.groupby("_key")
             .agg(title=("title", "first"),
                  author=("author", "first"),
                  year=("publication_year", "min"),
                  lang=("language", "first"),
                  rights=("rights", "first"),
                  fmt=("bib_fmt", "first"),
                  publisher=("publisher", "first"),
                  oclc=("oclc_num", "first"),
                  htid=("id", "first"),
                  url=("source_url", "first"),
                  n_copies=("id", "size"))
             .sort_values("n_copies", ascending=False)
             .reset_index(drop=True))

    print(f"distinct works after dedup : {len(agg):,}")
    print(f"collapse ratio for this author: {len(m)/len(agg):.2f}x")

    agg.to_parquet(f"data/experiment/{args.out}.parquet", index=False)
    agg.to_csv(f"data/experiment/{args.out}.csv", index=False)
    print(f"\nwrote data/experiment/{args.out}.{{parquet,csv}}")

    pd.set_option("display.width", 250, "display.max_colwidth", 62,
                  "display.max_rows", 200)
    print(f"\n{'='*150}\nDEDUPLICATED DATAFRAME  (shape {agg.shape})\n{'='*150}")
    print(agg[["title", "author", "year", "lang", "rights", "n_copies"]].to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
