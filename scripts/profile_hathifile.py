"""Profile every column of a hathifile: fill rate, cardinality, and value distribution.

Cardinality is tracked with hashed values and a per-column cap, so memory stays
bounded on the high-cardinality identifier columns. Columns whose distinct count
stays small keep a full value counter, which is what turns them into documented
controlled vocabularies.
"""
import csv, gzip, json, sys
from collections import Counter

HATHI = "data/experiment/hathi_full_20260801.txt.gz"
COLS = [
    "htid", "access", "rights", "ht_bib_key", "description", "source",
    "source_bib_num", "oclc_num", "isbn", "issn", "lccn", "title", "imprint",
    "rights_reason_code", "rights_timestamp", "us_gov_doc_flag", "rights_date_used",
    "pub_place", "lang", "bib_fmt", "collection_code", "content_provider_code",
    "responsible_entity_code", "digitization_agent_code", "access_profile_code", "author",
]
CARD_CAP = 1_000_000     # stop counting distinct beyond this
VOCAB_CAP = 400          # above this many distinct values, stop keeping a counter

n = len(COLS)
nonempty = [0] * n
distinct = [set() for _ in range(n)]
capped = [False] * n
vocab = [Counter() for _ in range(n)]
vocab_open = [True] * n
minlen = [10**9] * n
maxlen = [0] * n
samples = [[] for _ in range(n)]
rows = 0

with gzip.open(HATHI, "rt", encoding="utf-8", errors="replace") as fh:
    for f in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(f) < n:
            continue
        rows += 1
        for i in range(n):
            v = f[i].strip()
            if not v:
                continue
            nonempty[i] += 1
            L = len(v)
            if L < minlen[i]: minlen[i] = L
            if L > maxlen[i]: maxlen[i] = L
            if not capped[i]:
                distinct[i].add(hash(v))
                if len(distinct[i]) >= CARD_CAP:
                    capped[i] = True   # stop growing; len() is now the cap marker
            if vocab_open[i]:
                vocab[i][v] += 1
                if len(vocab[i]) > VOCAB_CAP:
                    vocab_open[i] = False
                    vocab[i] = Counter()
            if len(samples[i]) < 3 and L < 80:
                samples[i].append(v)
        if rows % 8_000_000 == 0:
            print(f"  ...{rows:,} rows", flush=True)

out = {"rows": rows, "columns": []}
for i, name in enumerate(COLS):
    card = f">{CARD_CAP:,}" if capped[i] else f"{len(distinct[i]):,}"
    out["columns"].append({
        "position": i + 1,
        "name": name,
        "fill_pct": round(100 * nonempty[i] / rows, 2),
        "distinct": card,
        "min_len": minlen[i] if minlen[i] < 10**9 else 0,
        "max_len": maxlen[i],
        "controlled_vocab": (
            [[v, c] for v, c in vocab[i].most_common(40)] if vocab_open[i] else None
        ),
        "samples": samples[i],
    })

json.dump(out, open("data/experiment/hathi_column_profile.json", "w"), indent=2)
print(f"\nrows profiled: {rows:,}")
for c in out["columns"]:
    kind = "VOCAB" if c["controlled_vocab"] is not None else "free/id"
    print(f"  {c['position']:>2} {c['name']:<24} fill {c['fill_pct']:>6.2f}%  "
          f"distinct {c['distinct']:>12}  {kind}")
print("\nwrote data/experiment/hathi_column_profile.json")
