"""Refine the overlap estimate by requiring the author to agree, not just the title.

Title-only matching is unreliable in both directions: generic titles ("Poems")
collide across thousands of unrelated works, while title variants between the two
cataloguing conventions cause misses. Adding the first author's surname removes
most of the false positives and gives a defensible lower bound.
"""
import csv, gzip, json, sys
from collections import Counter, defaultdict
sys.path.insert(0, "scripts")
from compare_overlap import norm_main, norm_full

GUT = "data/experiment/gutenberg_all.jsonl"
HATHI = "data/experiment/hathi_full_20260801.txt.gz"
PD = {"pd", "pdus", "pd-pvt"}


def surname(name):
    """MARC-style 'Surname, Forename, dates' -> 'surname' (both sources use it)."""
    if not name:
        return ""
    return name.split(",")[0].strip().lower()


gut = []
with open(GUT, encoding="utf-8") as f:
    for line in f:
        r = json.loads(line)
        authors = r.get("authors") or []
        gut.append((r.get("id"), r.get("title") or "",
                    surname(authors[0]["name"]) if authors else ""))

with_author = [g for g in gut if g[2]]
print(f"Gutenberg records            : {len(gut):,}")
print(f"  with at least one author   : {len(with_author):,} "
      f"({100*len(with_author)/len(gut):.1f}%)")

# title -> gutenberg ids  (for measuring generic-title collisions)
title_idx = defaultdict(list)
# (title, surname) -> gutenberg ids
pair_idx = defaultdict(list)
for gid, title, sn in gut:
    t = norm_main(title)
    if t:
        title_idx[t].append(gid)
        if sn:
            pair_idx[(t, sn)].append(gid)

print(f"  distinct (title, author)   : {len(pair_idx):,}\n")

title_hits = Counter()
pair_hits = Counter()
pair_pd = set()
rows = 0
with gzip.open(HATHI, "rt", encoding="utf-8", errors="replace") as fh:
    for f in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(f) < 26:
            continue
        rows += 1
        t = norm_main(f[11])
        if t in title_idx:
            title_hits[t] += 1
            key = (t, surname(f[25]))
            if key in pair_idx:
                pair_hits[key] += 1
                if f[2] in PD:
                    pair_pd.add(key)
        if rows % 8000000 == 0:
            print(f"  ...{rows:,} rows", flush=True)

print(f"HathiTrust rows scanned      : {rows:,}\n")

matched_books = sum(len(pair_idx[k]) for k in pair_hits)
pd_books = sum(len(pair_idx[k]) for k in pair_pd)
base = len(with_author)

print("--- TITLE + AUTHOR MATCH (high confidence) ---")
print(f"  Gutenberg books matched    : {matched_books:,} / {base:,} of authored books "
      f"({100*matched_books/base:.1f}%)")
print(f"  as share of all {len(gut):,}    : {100*matched_books/len(gut):.1f}%")
print(f"  HathiTrust copy is PD      : {pd_books:,} ({100*pd_books/base:.1f}% of authored)")

# How much of the title-only result was generic-title noise?
generic = {t for t, c in title_hits.items() if c >= 50}
generic_books = sum(len(title_idx[t]) for t in generic)
print(f"\n--- FALSE-POSITIVE PRESSURE IN TITLE-ONLY MATCHING ---")
print(f"  matched titles hitting >=50 HathiTrust volumes: {len(generic):,}")
print(f"  Gutenberg books behind them                   : {generic_books:,}")
print(f"  (these are the ones title-only matching inflates)")

json.dump({
    "gutenberg_total": len(gut),
    "gutenberg_with_author": base,
    "title_author_matched": matched_books,
    "title_author_matched_pd": pd_books,
    "generic_titles": len(generic),
}, open("data/experiment/overlap_results2.json", "w"), indent=2)
print("\nwrote data/experiment/overlap_results2.json")
