"""Compare the Gutenberg catalog against the HathiTrust monthly file by title.

Builds a lookup from the ~80k Gutenberg titles (small), then streams the ~19.6M
HathiTrust rows once and marks which Gutenberg titles are found. Memory stays flat
regardless of how large the HathiTrust file is.

Two normalizations are reported, because the two sources title things differently:

  FULL  lowercase, drop the statement of responsibility after " / ",
        strip punctuation and leading articles, collapse whitespace
  MAIN  as above, then also cut at the first ':' or ';' so only the main title
        remains ("Moby Dick; Or, The Whale" -> "moby dick")

MAIN finds more matches but risks false positives on generic titles, so both are
reported side by side.
"""
import csv, gzip, json, re, sys
from collections import Counter, defaultdict

GUT = "data/experiment/gutenberg_all.jsonl"
HATHI = "data/experiment/hathi_full_20260801.txt.gz"
PD_RIGHTS = {"pd", "pdus", "pd-pvt"}

ARTICLES = ("the ", "a ", "an ", "le ", "la ", "les ", "el ", "il ",
            "der ", "die ", "das ", "een ", "de ", "het ")
PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
# ":" / ";" subtitle, or the bibliographic "Title, or, Alternative" convention.
ALT_TITLE = re.compile(r"\s*[:;]\s*|\s*[,;:]\s*or\s*[,.]?\s+", re.IGNORECASE)
WS = re.compile(r"\s+")


def norm_full(title):
    t = (title or "").lower().replace("\r", " ").replace("\n", " ")
    t = t.split(" / ")[0]              # HathiTrust statement of responsibility
    t = PUNCT.sub(" ", t)
    t = WS.sub(" ", t).strip()
    for a in ARTICLES:
        if t.startswith(a):
            t = t[len(a):]
            break
    return t.strip()


def norm_main(title):
    t = (title or "").lower().replace("\r", " ").replace("\n", " ")
    t = t.split(" / ")[0]
    # Drop the subtitle / alternative title. Both punctuation conventions must be
    # handled or the split is asymmetric: Gutenberg writes "Moby Dick; Or, The
    # Whale" but HathiTrust writes "Moby-Dick, or, The whale", and cutting only
    # one of them turns a real match into a miss.
    t = ALT_TITLE.split(t)[0]
    t = PUNCT.sub(" ", t)
    t = WS.sub(" ", t).strip()
    for a in ARTICLES:
        if t.startswith(a):
            t = t[len(a):]
            break
    return t.strip()


def main():
    # ---- load Gutenberg ----
    gut = []
    with open(GUT, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            gut.append((r.get("id"), r.get("title") or ""))
    print(f"Gutenberg records loaded : {len(gut):,}")

    by_full, by_main = defaultdict(list), defaultdict(list)
    for gid, title in gut:
        nf, nm = norm_full(title), norm_main(title)
        if nf:
            by_full[nf].append(gid)
        if nm:
            by_main[nm].append(gid)
    print(f"  distinct FULL titles   : {len(by_full):,}")
    print(f"  distinct MAIN titles   : {len(by_main):,}")

    # ---- stream HathiTrust ----
    hit_full, hit_main = Counter(), Counter()
    hit_full_pd, hit_main_pd = set(), set()
    rows = 0
    print("\nstreaming HathiTrust ...", flush=True)
    with gzip.open(HATHI, "rt", encoding="utf-8", errors="replace") as fh:
        for f in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
            if len(f) < 26:
                continue
            rows += 1
            is_pd = f[2] in PD_RIGHTS
            nf = norm_full(f[11])
            if nf in by_full:
                hit_full[nf] += 1
                if is_pd:
                    hit_full_pd.add(nf)
            nm = norm_main(f[11])
            if nm in by_main:
                hit_main[nm] += 1
                if is_pd:
                    hit_main_pd.add(nm)
            if rows % 5000000 == 0:
                print(f"  ...{rows:,} rows", flush=True)
    print(f"HathiTrust rows scanned  : {rows:,}")

    # ---- results ----
    def report(label, index, hits, pd_hits):
        matched_titles = len(hits)
        matched_books = sum(len(index[t]) for t in hits)
        pd_books = sum(len(index[t]) for t in pd_hits)
        total_books = sum(len(v) for v in index.values())
        print(f"\n--- {label} TITLE MATCH ---")
        print(f"  Gutenberg titles matched : {matched_titles:,} / {len(index):,} "
              f"({100*matched_titles/len(index):.1f}%)")
        print(f"  Gutenberg books covered  : {matched_books:,} / {total_books:,} "
              f"({100*matched_books/total_books:.1f}%)")
        print(f"  ...where the HathiTrust copy is public domain: {pd_books:,} "
              f"({100*pd_books/total_books:.1f}%)")
        return matched_books, total_books

    report("FULL", by_full, hit_full, hit_full_pd)
    report("MAIN", by_main, hit_main, hit_main_pd)

    # ---- qualitative samples ----
    gid_title = dict(gut)
    unmatched = [t for t in by_main if t not in hit_main]
    print("\n--- 15 UNMATCHED Gutenberg titles (MAIN) ---")
    for t in unmatched[:15]:
        print(f"  {gid_title.get(by_main[t][0], '')[:95]}")

    print("\n--- 10 most-duplicated matches (generic-title false-positive risk) ---")
    for t, c in hit_main.most_common(10):
        print(f"  {c:>7,} HathiTrust vols : {t[:70]!r}")

    with open("data/experiment/overlap_results.json", "w") as f:
        json.dump({
            "gutenberg_records": len(gut),
            "hathitrust_rows": rows,
            "full_titles_matched": len(hit_full),
            "full_titles_total": len(by_full),
            "main_titles_matched": len(hit_main),
            "main_titles_total": len(by_main),
            "unmatched_main_sample": [gid_title.get(by_main[t][0], "") for t in unmatched[:200]],
        }, f, indent=2)
    print("\nwrote data/experiment/overlap_results.json")


if __name__ == "__main__":
    sys.exit(main())
