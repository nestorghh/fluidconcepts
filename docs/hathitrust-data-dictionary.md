# HathiTrust hathifiles — Data Dictionary

Every figure below was measured against `hathi_full_20260801.txt.gz`:
**19,605,849 rows**, all 26 columns profiled. Fill rates and cardinalities are
observed, not documented claims.

## File mechanics

| | |
|---|---|
| Format | gzipped TSV, **no header row** |
| Columns | 26, fixed order (positional — the order below *is* the schema) |
| Monthly full | `hathi_full_YYYYMM01.txt.gz`, ~1.22 GB, complete snapshot |
| Daily update | `hathi_upd_YYYYMMDD.txt.gz`, ~6–8 MB, changed/new records only |
| Index | `hathi_file_list.json` — lists what currently exists |

**Parsing gotcha:** titles contain bare `"` characters. Parse with
`csv.QUOTE_NONE` or the default quoting will silently mis-split rows.

## Column reference

| # | Column | Fill | Distinct | Notes |
|---|---|---|---|---|
| 1 | `htid` | 100% | >1M | **Primary key.** Unique per row; nothing else is |
| 2 | `access` | 100% | 2 | `allow` / `deny` — viewability, not rights |
| 3 | `rights` | 100% | 23 | Copyright determination. See vocabulary below |
| 4 | `ht_bib_key` | 100% | >1M | HathiTrust bib record id; 9 chars, zero-padded |
| 5 | `description` | 50.2% | >1M | Volume/enumeration (`v.5`, `v.1`), not a summary |
| 6 | `source` | 100% | 71 | Contributing institution (`MIU`, `UC`, `HVD`) |
| 7 | `source_bib_num` | 100% | >1M | Local catalog number at the source library |
| 8 | `oclc_num` | 95.1% | >1M | WorldCat id. **Multi-valued in ~15.7% of rows** |
| 9 | `isbn` | 14.4% | >1M | Multi-valued; up to 5,902 chars |
| 10 | `issn` | 16.8% | 113,548 | Serials mostly |
| 11 | `lccn` | 52.1% | >1M | LC **control number** — an id, *not* a call number |
| 12 | `title` | 100% | >1M | Includes `/ statement of responsibility`; ≤3,142 chars |
| 13 | `imprint` | 94.8% | >1M | Publisher + date as one string |
| 14 | `rights_reason_code` | 100% | 16 | Why the rights value was assigned |
| 15 | `rights_timestamp` | 100% | 267,968 | `YYYY-MM-DD HH:MM:SS`, when rights were set |
| 16 | `us_gov_doc_flag` | 100% | 2 | `1` = US government document (6.65%) |
| 17 | `rights_date_used` | 100% | 972 | Publication year — **but see the 9999 caveat** |
| 18 | `pub_place` | 99.0% | 1,493 | 3-letter MARC country code (`nyu`, `xo`) |
| 19 | `lang` | 99.4% | 660 | 3-letter MARC language code (`eng`, `ger`) |
| 20 | `bib_fmt` | 100% | 8 | **Material type — the "is it a book" field** |
| 21 | `collection_code` | 100% | 98 | Holding collection |
| 22 | `content_provider_code` | 100% | 75 | Provider (`umich`, `harvard`) |
| 23 | `responsible_entity_code` | 100% | 69 | Entity responsible for the volume |
| 24 | `digitization_agent_code` | 100% | 65 | Who scanned it (94% `google`) |
| 25 | `access_profile_code` | 100% | 4 | `google` 88.9%, `open` 11.0% |
| 26 | `author` | 65.4% | >1M | `Surname, Forename, dates`; ≤1,026 chars |

## Controlled vocabularies

### `bib_fmt` — material type
| Code | Meaning | Rows | % |
|---|---|---:|---:|
| `BK` | **Book** | 12,787,978 | 65.23% |
| `SE` | Serial (journal, annual, periodical) | 6,581,932 | 33.57% |
| `MU` | Music / score | 227,226 | 1.16% |
| `MP` | Map | 6,077 | 0.03% |
| `MX` | Mixed materials (archival) | 1,972 | 0.01% |
| `VM` | Visual material | 575 | 0.00% |
| `CF` | Computer file | 81 | 0.00% |
| `XX` | Unknown | 8 | 0.00% |

### `rights` — copyright status (23 values)
| Code | Meaning | Rows | % | Public domain? |
|---|---|---:|---:|---|
| `ic` | in copyright | 10,311,741 | 52.60% | no |
| `pd` | public domain | 5,839,738 | 29.79% | **yes** |
| `pdus` | public domain in the US | 2,288,714 | 11.67% | **yes** |
| `und` | undetermined | 963,937 | 4.92% | no |
| `icus` | in copyright in the US | 69,687 | 0.36% | no |
| `cc-by-4.0` | Creative Commons BY | 28,086 | 0.14% | open, not PD |
| `cc-by-nc-nd-4.0` | CC BY-NC-ND | 22,119 | 0.11% | open, not PD |
| `und-world` | undetermined, world access | 21,218 | 0.11% | no |
| `cc-by-nc-4.0` | CC BY-NC | 18,326 | 0.09% | open, not PD |
| `cc-zero` | CC0 dedication | 12,942 | 0.07% | effectively |
| `ic-world` | in copyright, world viewable | 7,655 | 0.04% | no |
| `op` | out of print | 4,213 | 0.02% | no |
| | *plus `pd-pvt` and 9 rarer codes* | | | |

Public domain = `{pd, pdus, pd-pvt}` → **8,129,991 rows (41.5%)**.
`cc-*` codes are openly licensed but legally distinct from public domain.

### `rights_reason_code` — why that determination
`bib` 91.78% · `gfv` 1.87% · `ren` 1.49% · `ncn` 1.33% · `add` 1.03% ·
`nfi` 0.67% · `con` 0.64% · `gatt` 0.36% · `cdpp` 0.34% · `exp` 0.26% ·
`crms` 0.10% · `man` 0.09% · plus 4 rarer.

### `access` / `access_profile_code` / `us_gov_doc_flag`
- `access`: `deny` 57.90%, `allow` 42.10%
- `access_profile_code`: `google` 88.94%, `open` 10.95%, `page+lowres` 0.11%, `page` 0.01%
- `us_gov_doc_flag`: `0` 93.35%, `1` 6.65% (1,303,244 government documents)

### `source` — top contributors
`MIU` 25.44% · `UC` 24.68% · `UIU` 5.82% · `HVD` 5.13% · `COO` 4.54% ·
`UMN` 3.18% · `UVA` 3.11% · `NYP` 3.08% · `WU` 2.92% · `INU` 2.83% —
plus 61 more. Michigan and California together are **half the corpus**.

## Data quality warnings

**1. `rights_date_used` uses 9999 as a sentinel.**
1,477,418 rows (**7.54%**) carry `9999`, meaning unknown — not a year.
Plausible years (1000–2026) cover 92.46%. Filter the sentinel before any
date arithmetic or averaging, or results will be badly skewed.

**2. `oclc_num` is multi-valued.** ~15.7% of rows look like `"217097625,252"`.
Split on comma, or match on the first value. In practice raw-vs-first differed
by only 149 out of 8.5M distinct book values.

**3. `oclc_num` is not unique per row** — it identifies a work/edition. Several
`htid`s legitimately share one OCLC (different libraries' scans of one book).
It is missing entirely from 952,005 rows (4.9%), so it cannot be the sole key.

**4. Rows are volumes, not works.** Collapse ratios (rows per distinct work):

| Format | all rows | public domain |
|---|---|---|
| Book | 1.50x | 1.75x |
| **Serial** | **13.88x** | **15.76x** |
| Music | 1.28x | 1.22x |

Serials inflate row counts by an order of magnitude — every issue of a journal
shares one OCLC.

**5. `description` is not a description.** It holds volume enumeration
(`v.5`, `no.3`). There is no abstract or summary field anywhere in the file.

## What is NOT in this dataset

No subject headings, no LC classification/call numbers, no genre, no abstracts,
no page counts, no full text. `bib_fmt` gives **material type** (book vs serial
vs map) but nothing finer — poetry vs fiction vs research is **not derivable**
from these files. Genre requires enrichment via `oclc_num`/`lccn` against
WorldCat or the Library of Congress, or the HathiTrust Bib API (one request per
record).

## Practical recipes

```python
PD = {"pd", "pdus", "pd-pvt"}

# public domain books, excluding government documents
row[2] in PD and row[19] == "BK" and row[15] != "1"      # 4,624,169 rows

# distinct public-domain books (deduplicated by work)
#   ~3,011,194 by OCLC   /   ~2,947,973 by title+author

# usable publication year
year = row[16]
if year.isdigit() and 1000 <= int(year) <= 2026: ...     # excludes the 9999 sentinel

# English-language public-domain books
row[2] in PD and row[19] == "BK" and row[18] == "eng"    # 3,175,913 rows
```

Column positions are 0-indexed here to match `csv.reader` output.
