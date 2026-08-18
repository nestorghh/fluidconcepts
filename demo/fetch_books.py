"""Demo: fetch public domain book metadata, save as JSON + CSV + Parquet.

Two sources:

    # Project Gutenberg (REST API, needs a key)
    export RAPIDAPI_KEY=your-key
    python demo/fetch_books.py 50

    # HathiTrust (bulk TSV files, no key, no signup)
    python demo/fetch_books.py 50 --source hathitrust

Writes into demo/output/:
    books_<source>_raw.json    the untouched source records
    books_<source>.csv         one row per book, one column per metadata field
    books_<source>.parquet     the same table in Parquet
"""

import argparse
import csv
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import pandas as pd

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# --- Gutenberg (API) ---
GUTENBERG_HOST = "project-gutenberg-free-books-api1.p.rapidapi.com"
PAGE_SIZE = 100            # the API's maximum
SLEEP_BETWEEN_PAGES = 12   # the free plan returns 429 on back-to-back requests

# --- HathiTrust (files) ---
# Bulk TSV dumps, no API and no key. A full file is published monthly (~1.2 GB
# gzipped, ~19M rows); an update file is published daily (~6 MB). The JSON index
# lists both. Note hathitrust.org's HTML pages block scripted requests (403), but
# these static file URLs serve fine.
HATHI_INDEX = "https://www.hathitrust.org/files/hathifiles/hathi_file_list.json"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"

# Hathifiles have no header row; these are the 26 columns in order.
HATHI_COLUMNS = [
    "htid", "access", "rights", "ht_bib_key", "description", "source",
    "source_bib_num", "oclc_num", "isbn", "issn", "lccn", "title", "imprint",
    "rights_reason_code", "rights_timestamp", "us_gov_doc_flag", "rights_date_used",
    "pub_place", "lang", "bib_fmt", "collection_code", "content_provider_code",
    "responsible_entity_code", "digitization_agent_code", "access_profile_code", "author",
]

# The rights codes that mean public domain. Verified against a live file:
#   pd (public domain), pdus (public domain in the US), pd-pvt (public domain,
#   private access). Everything else -- ic, und, op, icus, nobody -- is not.
# cc-* codes are openly licensed but NOT public domain, so they are excluded here.
PUBLIC_DOMAIN_RIGHTS = {"pd", "pdus", "pd-pvt"}


# ---------------------------------------------------------------- Gutenberg

def fetch_gutenberg(n, api_key):
    """Fetch n books from the API, in ascending Gutenberg ID order."""
    books = []
    page = 1

    while len(books) < n:
        params = urllib.parse.urlencode(
            {"page": page, "page_size": PAGE_SIZE, "sort": "ascending"}
        )
        request = urllib.request.Request(
            f"https://{GUTENBERG_HOST}/books?{params}",
            headers={"x-rapidapi-key": api_key, "x-rapidapi-host": GUTENBERG_HOST},
        )

        print(f"  fetching page {page} ...")
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)

        results = payload.get("results", [])
        if not results:
            break
        books.extend(results)

        if not payload.get("next"):
            break
        page += 1
        if len(books) < n:
            time.sleep(SLEEP_BETWEEN_PAGES)

    return books[:n]


def gutenberg_rows(books):
    """One row per book. List fields are joined so CSV stays readable."""
    rows = []
    for book in books:
        formats = book.get("formats") or {}
        rows.append({
            "source": "gutenberg",
            "id": book.get("id"),
            "title": book.get("title"),
            "authors": "; ".join(a["name"] for a in book.get("authors") or []),
            "subjects": "; ".join(book.get("subjects") or []),
            "bookshelves": "; ".join(book.get("bookshelves") or []),
            "summary": book.get("summary"),
            "download_count": book.get("download_count"),
            "reading_ease_score": book.get("reading_ease_score"),
            "issued": book.get("issued"),
            "rights": "pd",
            "is_public_domain": True,
            "cover_image": book.get("cover_image"),
            "text_url": formats.get("text/plain"),
            "source_url": f"https://www.gutenberg.org/ebooks/{book.get('id')}",
        })
    return rows


# -------------------------------------------------------------- HathiTrust

def latest_hathi_file(want_full):
    """Pick the newest full (monthly) or update (daily) file from the JSON index."""
    request = urllib.request.Request(HATHI_INDEX, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(request, timeout=60) as response:
        files = json.load(response)

    matching = [f for f in files if bool(f.get("full")) == want_full]
    newest = max(matching, key=lambda f: f["created"])
    return newest


def fetch_hathitrust(n, want_full=False, public_domain_only=True):
    """Stream a hathifile and keep the first n rows that match.

    The gzip is decoded as it downloads and stopped as soon as n rows are found,
    so a 1.2 GB full file never has to land on disk.
    """
    meta = latest_hathi_file(want_full)
    size_mb = meta.get("size", 0) / 1e6
    print(f"  file: {meta['filename']} ({size_mb:,.0f} MB, created {meta['created'][:10]})")
    print("  streaming (stops as soon as enough rows are found) ...")

    request = urllib.request.Request(meta["url"], headers={"User-Agent": BROWSER_UA})
    books, scanned = [], 0

    with urllib.request.urlopen(request, timeout=120) as response:
        with gzip.open(response, "rt", encoding="utf-8", errors="replace") as handle:
            # QUOTE_NONE matters: titles contain bare quote characters.
            for fields in csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE):
                if len(fields) < len(HATHI_COLUMNS):
                    continue
                scanned += 1
                record = dict(zip(HATHI_COLUMNS, fields))

                if public_domain_only and record["rights"] not in PUBLIC_DOMAIN_RIGHTS:
                    continue

                books.append(record)
                if len(books) >= n:
                    break

    print(f"  scanned {scanned:,} rows to find {len(books):,} matching")
    return books, meta["filename"]


def hathitrust_rows(books):
    """One row per volume, using the fields HathiTrust actually carries."""
    rows = []
    for book in books:
        htid = book["htid"]
        rows.append({
            "source": "hathitrust",
            "id": htid,
            "title": book["title"],
            "authors": book["author"],
            "publication_year": book["rights_date_used"],
            "publisher": book["imprint"],
            "language": book["lang"],
            "rights": book["rights"],
            "access": book["access"],
            "is_public_domain": book["rights"] in PUBLIC_DOMAIN_RIGHTS,
            "isbn": book["isbn"],
            "oclc_num": book["oclc_num"],
            "lccn": book["lccn"],
            "bib_fmt": book["bib_fmt"],
            "pub_place": book["pub_place"],
            "holding_library": book["source"],
            "source_url": f"https://babel.hathitrust.org/cgi/pt?id={urllib.parse.quote(htid)}",
            "catalog_url": f"https://catalog.hathitrust.org/Record/{book['ht_bib_key']}",
        })
    return rows


# -------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("n", nargs="?", type=int, default=50, help="how many books")
    parser.add_argument("--source", choices=["gutenberg", "hathitrust"], default="gutenberg")
    parser.add_argument("--full", action="store_true",
                        help="hathitrust: use the monthly full file instead of the daily update")
    parser.add_argument("--all-rights", action="store_true",
                        help="hathitrust: keep every record, not just public domain ones")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"Fetching {args.n} books from {args.source}...")
    provenance = None

    if args.source == "gutenberg":
        api_key = os.environ.get("RAPIDAPI_KEY")
        if not api_key:
            print("error: set RAPIDAPI_KEY first")
            return 1
        books = fetch_gutenberg(args.n, api_key)
        rows = gutenberg_rows(books)
    else:
        books, provenance = fetch_hathitrust(
            args.n, want_full=args.full, public_domain_only=not args.all_rights
        )
        rows = hathitrust_rows(books)

    print(f"Got {len(books)} books.\n")

    prefix = os.path.join(OUTPUT_DIR, f"books_{args.source}")
    with open(f"{prefix}_raw.json", "w", encoding="utf-8") as f:
        json.dump(books, f, indent=2, ensure_ascii=False)

    df = pd.DataFrame(rows)
    df.to_csv(f"{prefix}.csv", index=False)
    df.to_parquet(f"{prefix}.parquet", index=False)

    print(f"raw json  -> {prefix}_raw.json")
    print(f"csv       -> {prefix}.csv")
    print(f"parquet   -> {prefix}.parquet")
    if provenance:
        print(f"from file -> {provenance}")

    print(f"\nDataFrame: {df.shape[0]} rows x {df.shape[1]} columns")
    print(f"Columns: {list(df.columns)}")
    if "rights" in df:
        print(f"\nRights codes: {df['rights'].value_counts().to_dict()}")
    print("\nFirst few rows:")
    cols = [c for c in ("id", "title", "authors", "publication_year", "rights") if c in df]
    print(df[cols].head().to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
