"""How many distinct BOOKS (not rows) lack an author?

A work is keyed by its first OCLC number; rows with no OCLC fall back to a
normalized title. Copies of one work are catalogued independently by different
libraries, so a work can have an author on some rows and not others - that case is
counted separately, because those authors are recoverable.
"""
import csv, gzip, sys
sys.path.insert(0, "scripts")
from compare_overlap import norm_full

PD = {"pd", "pdus", "pd-pvt"}
HATHI = "data/experiment/hathi_full_20260801.txt.gz"

# per scope: OCLCs seen with an author, and seen without one
has = {"all": set(), "pd": set()}
lacks = {"all": set(), "pd": set()}
# books with no OCLC at all, keyed on title
nooclc_has = {"all": set(), "pd": set()}
nooclc_lacks = {"all": set(), "pd": set()}
rows = {"all": 0, "pd": 0}

n = 0
with gzip.open(HATHI, "rt", encoding="utf-8", errors="replace") as fh:
    for f in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(f) < 26 or f[19] != "BK":
            continue
        n += 1
        scopes = ("all", "pd") if f[2] in PD else ("all",)
        author = bool(f[25].strip())
        o = f[7].strip()
        key = hash(o.split(",")[0].strip()) if o else None
        tkey = None if o else hash(norm_full(f[11]))

        for s in scopes:
            rows[s] += 1
            if key is not None:
                (has[s] if author else lacks[s]).add(key)
            else:
                (nooclc_has[s] if author else nooclc_lacks[s]).add(tkey)
        if n % 6000000 == 0:
            print(f"  ...{n:,} book rows", flush=True)


def report(scope, label):
    A, B = has[scope], lacks[scope]
    total = len(A | B)
    never = len(B - A)
    mixed = len(A & B)
    always = len(A - B)
    nA, nB = nooclc_has[scope], nooclc_lacks[scope]
    n_total = len(nA | nB)
    n_never = len(nB - nA)

    print(f"\n=== {label} ===")
    print(f"  book rows                          : {rows[scope]:,}")
    print(f"\n  distinct books (by OCLC)           : {total:,}")
    print(f"    author on every copy             : {always:,}  ({100*always/total:.1f}%)")
    print(f"    author on some copies only       : {mixed:,}  ({100*mixed/total:.1f}%)")
    print(f"    NO author on any copy            : {never:,}  ({100*never/total:.1f}%)")
    print(f"\n  books with no OCLC (keyed on title): {n_total:,}")
    print(f"    NO author on any copy            : {n_never:,}")
    print(f"\n  >>> TOTAL distinct books           : {total + n_total:,}")
    print(f"  >>> TOTAL with no author anywhere  : {never + n_never:,} "
          f"({100*(never+n_never)/(total+n_total):.1f}%)")
    print(f"  >>> recoverable from another copy  : {mixed:,}")


report("all", "ALL BOOKS (bib_fmt=BK)")
report("pd", "PUBLIC DOMAIN BOOKS")
