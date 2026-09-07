"""Extract distinct public-domain BOOKS that have no author, with all 26 columns.

Deduplicated to one row per work: keyed on the first OCLC number, falling back to a
normalized title when OCLC is absent. The surviving row is the first copy seen, plus
a derived n_copies count so the collapsed duplicates are still visible.
"""
import csv, gzip, sys
import pandas as pd

sys.path.insert(0, "scripts")
from compare_overlap import norm_full

HATHI = "data/experiment/hathi_full_20260801.txt.gz"
OUT = "demo/output/authorless_pd_books"
PD = {"pd", "pdus", "pd-pvt"}
COLS = [
    "htid", "access", "rights", "ht_bib_key", "description", "source",
    "source_bib_num", "oclc_num", "isbn", "issn", "lccn", "title", "imprint",
    "rights_reason_code", "rights_timestamp", "us_gov_doc_flag", "rights_date_used",
    "pub_place", "lang", "bib_fmt", "collection_code", "content_provider_code",
    "responsible_entity_code", "digitization_agent_code", "access_profile_code", "author",
]

works = {}      # key -> row list
copies = {}     # key -> count
n = 0
with gzip.open(HATHI, "rt", encoding="utf-8", errors="replace") as fh:
    for f in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(f) < 26:
            continue
        if f[19] != "BK" or f[2] not in PD or f[25].strip():
            continue
        n += 1
        o = f[7].strip()
        key = ("o", o.split(",")[0].strip()) if o else ("t", norm_full(f[11]))
        if key in works:
            copies[key] += 1
        else:
            works[key] = f[:26]
            copies[key] = 1
        if n % 1000000 == 0:
            print(f"  ...{n:,} matching rows", flush=True)

print(f"\nmatching rows      : {n:,}")
print(f"distinct works     : {len(works):,}")

df = pd.DataFrame([works[k] for k in works], columns=COLS)
df["n_copies"] = [copies[k] for k in works]
df["dedup_key"] = [f"{k[0]}:{k[1]}" for k in works]

# a couple of convenience columns for display / filtering
df["publication_year"] = pd.to_numeric(df["rights_date_used"], errors="coerce")
df.loc[(df.publication_year < 1000) | (df.publication_year > 2026), "publication_year"] = None
df["reader_url"] = "https://babel.hathitrust.org/cgi/pt?id=" + df["htid"]
df["catalog_url"] = "https://catalog.hathitrust.org/Record/" + df["ht_bib_key"]

df = df.sort_values("n_copies", ascending=False).reset_index(drop=True)
df.to_parquet(f"{OUT}.parquet", index=False)
df.to_csv(f"{OUT}.csv", index=False)

print(f"columns            : {len(df.columns)}")
print(f"\nwrote {OUT}.parquet  ({len(df):,} rows)")
print(f"wrote {OUT}.csv")
print(f"\nyear coverage      : {df.publication_year.notna().sum():,} "
      f"({100*df.publication_year.notna().mean():.1f}%)  "
      f"range {df.publication_year.min():.0f}-{df.publication_year.max():.0f}")
print(f"languages (top 5)  : {df.lang.value_counts().head().to_dict()}")
print(f"rights split       : {df.rights.value_counts().to_dict()}")
