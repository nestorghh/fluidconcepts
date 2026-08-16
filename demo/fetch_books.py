"""Demo: fetch N public domain books from Project Gutenberg, save as JSON + CSV + Parquet.

Usage:
    export RAPIDAPI_KEY=your-key
    python demo/fetch_books.py 50

Writes into demo/output/:
    books_raw.json    the untouched API response records
    books.csv         one row per book, one column per metadata field
    books.parquet     the same table in Parquet
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

import pandas as pd

HOST = "project-gutenberg-free-books-api1.p.rapidapi.com"
PAGE_SIZE = 100        # the API's maximum
SLEEP_BETWEEN_PAGES = 12   # the free plan returns 429 on back-to-back requests
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def fetch_books(n, api_key):
    """Fetch n books, paging through the catalog in ascending Gutenberg ID order."""
    books = []
    page = 1

    while len(books) < n:
        params = urllib.parse.urlencode(
            {"page": page, "page_size": PAGE_SIZE, "sort": "ascending"}
        )
        request = urllib.request.Request(
            f"https://{HOST}/books?{params}",
            headers={"x-rapidapi-key": api_key, "x-rapidapi-host": HOST},
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


def to_dataframe(books):
    """One row per book. List/dict fields are flattened so CSV stays readable."""
    rows = []
    for book in books:
        formats = book.get("formats") or {}
        rows.append(
            {
                "id": book.get("id"),
                "title": book.get("title"),
                "authors": "; ".join(a["name"] for a in book.get("authors") or []),
                "subjects": "; ".join(book.get("subjects") or []),
                "bookshelves": "; ".join(book.get("bookshelves") or []),
                "summary": book.get("summary"),
                "download_count": book.get("download_count"),
                "reading_ease_score": book.get("reading_ease_score"),
                "issued": book.get("issued"),
                "cover_image": book.get("cover_image"),
                "text_url": formats.get("text/plain"),
                "source_url": f"https://www.gutenberg.org/ebooks/{book.get('id')}",
                "removed_from_catalog": book.get("removed_from_catalog"),
            }
        )
    return pd.DataFrame(rows)


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50

    api_key = os.environ.get("RAPIDAPI_KEY")
    if not api_key:
        print("error: set RAPIDAPI_KEY first")
        return 1

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"Fetching {n} books...")
    books = fetch_books(n, api_key)
    print(f"Got {len(books)} books.\n")

    raw_path = os.path.join(OUTPUT_DIR, "books_raw.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(books, f, indent=2, ensure_ascii=False)

    df = to_dataframe(books)
    csv_path = os.path.join(OUTPUT_DIR, "books.csv")
    parquet_path = os.path.join(OUTPUT_DIR, "books.parquet")
    df.to_csv(csv_path, index=False)
    df.to_parquet(parquet_path, index=False)

    print(f"raw json  -> {raw_path}")
    print(f"csv       -> {csv_path}")
    print(f"parquet   -> {parquet_path}")
    print(f"\nDataFrame: {df.shape[0]} rows x {df.shape[1]} columns")
    print(f"Columns: {list(df.columns)}")
    print(f"\nWith a summary: {df['summary'].notna().sum()} / {len(df)}")
    print("\nFirst few rows:")
    print(df[["id", "title", "authors", "download_count"]].head())
    return 0


if __name__ == "__main__":
    sys.exit(main())
