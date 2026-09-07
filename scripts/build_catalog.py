"""Download both catalogs once, then combine them into one queryable table.

Three stages, each independently cached - nothing re-downloads unless you pass
--force:

    python scripts/build_catalog.py --download    # fetch both sources (slow, once)
    python scripts/build_catalog.py --build       # merge into combined.parquet
    python scripts/build_catalog.py --stats       # sanity-check the result

Then query it from anywhere:

    from scripts.build_catalog import load_combined
    df = load_combined(bib_fmt="BK", public_domain=True, columns=["source","title"])

The combined table is ~19.7M rows, which is too much to hold in pandas casually, so
load_combined() pushes filters down into the Parquet reader and only materializes
what matches. Reading everything is possible but expects several GB of RAM.
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

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

DATA_DIR = "data/experiment"
GUT_RAW = f"{DATA_DIR}/gutenberg_all.jsonl"
GUT_STATE = f"{DATA_DIR}/gutenberg_all.state.json"
COMBINED = f"{DATA_DIR}/combined.parquet"
MANIFEST = f"{DATA_DIR}/manifest.json"

GUT_HOST = "project-gutenberg-free-books-api1.p.rapidapi.com"
GUT_PAGE_SIZE = 100
GUT_SLEEP = 1.5
QUOTA_FLOOR = 15

HATHI_INDEX = "https://www.hathitrust.org/files/hathifiles/hathi_file_list.json"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36")
HATHI_COLS = [
    "htid", "access", "rights", "ht_bib_key", "description", "source",
    "source_bib_num", "oclc_num", "isbn", "issn", "lccn", "title", "imprint",
    "rights_reason_code", "rights_timestamp", "us_gov_doc_flag", "rights_date_used",
    "pub_place", "lang", "bib_fmt", "collection_code", "content_provider_code",
    "responsible_entity_code", "digitization_agent_code", "access_profile_code", "author",
]
PD_RIGHTS = {"pd", "pdus", "pd-pvt"}

# One schema for both sources. Fields a source cannot supply stay null rather than
# being faked, so "missing" is always distinguishable from "zero" or "unknown".
SCHEMA = pa.schema([
    ("source", pa.string()),            # "gutenberg" | "hathitrust"
    ("id", pa.string()),                # gutenberg id, or htid
    ("title", pa.string()),
    ("author", pa.string()),
    ("rights", pa.string()),
    ("is_public_domain", pa.bool_()),
    ("bib_fmt", pa.string()),
    ("access", pa.string()),
    ("language", pa.string()),
    ("publication_year", pa.int32()),
    ("publisher", pa.string()),
    ("oclc_num", pa.string()),
    ("isbn", pa.string()),
    ("lccn", pa.string()),
    ("subjects", pa.string()),          # gutenberg only
    ("summary", pa.string()),           # gutenberg only
    ("download_count", pa.int64()),     # gutenberg only
    ("source_url", pa.string()),
])
BATCH = 500_000


# ----------------------------------------------------------------- manifest

def read_manifest():
    return json.load(open(MANIFEST)) if os.path.exists(MANIFEST) else {}


def write_manifest(**kw):
    m = read_manifest()
    m.update(kw)
    json.dump(m, open(MANIFEST, "w"), indent=2)


# ----------------------------------------------------------------- download

def newest_hathi_full():
    req = urllib.request.Request(HATHI_INDEX, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        files = json.load(r)
    return max((f for f in files if f.get("full")), key=lambda f: f["created"])


def download_hathitrust(force=False):
    """Fetch the newest monthly full file. Free - no key, no quota."""
    meta = newest_hathi_full()
    path = f"{DATA_DIR}/{meta['filename']}"
    if os.path.exists(path) and not force:
        print(f"  hathitrust: cached {meta['filename']} "
              f"({os.path.getsize(path)/1e9:.2f} GB) - skipping")
        write_manifest(hathi_file=meta["filename"], hathi_created=meta["created"])
        return path

    print(f"  hathitrust: downloading {meta['filename']} ({meta['size']/1e9:.2f} GB) ...")
    req = urllib.request.Request(meta["url"], headers={"User-Agent": BROWSER_UA})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=3600) as r, open(path, "wb") as out:
        while chunk := r.read(1 << 20):
            out.write(chunk)
    print(f"  hathitrust: done in {time.time()-t0:.0f}s")
    write_manifest(hathi_file=meta["filename"], hathi_created=meta["created"])
    return path


def download_gutenberg(force=False):
    """Page the whole Gutenberg catalog. Resumable; ~790 requests of a 1000/mo quota."""
    if os.path.exists(GUT_RAW) and not force:
        n = sum(1 for _ in open(GUT_RAW, encoding="utf-8"))
        print(f"  gutenberg : cached {n:,} records - skipping")
        write_manifest(gutenberg_records=n)
        return GUT_RAW

    key = os.environ.get("RAPIDAPI_KEY")
    if not key:
        raise SystemExit("error: RAPIDAPI_KEY not set (needed for the Gutenberg download)")

    if force and os.path.exists(GUT_RAW):
        os.remove(GUT_RAW)
        if os.path.exists(GUT_STATE):
            os.remove(GUT_STATE)

    state = json.load(open(GUT_STATE)) if os.path.exists(GUT_STATE) else {"last_page": 0, "records": 0}
    page, total = state["last_page"] + 1, state["records"]
    out = open(GUT_RAW, "a", encoding="utf-8")
    quota = None
    print(f"  gutenberg : downloading from page {page} ...")

    while True:
        params = urllib.parse.urlencode(
            {"page": page, "page_size": GUT_PAGE_SIZE, "sort": "ascending"})
        req = urllib.request.Request(
            f"https://{GUT_HOST}/books?{params}",
            headers={"x-rapidapi-key": key, "x-rapidapi-host": GUT_HOST})
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    quota = r.headers.get("x-ratelimit-requests-remaining")
                    payload = json.load(r)
                break
            except Exception as exc:
                if attempt == 5:
                    out.close()
                    raise SystemExit(f"failed on page {page}: {exc}")
                time.sleep(5 * (attempt + 1))

        results = payload.get("results") or []
        for rec in results:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
        total += len(results)
        json.dump({"last_page": page, "records": total}, open(GUT_STATE, "w"))

        if page % 50 == 0:
            print(f"    page {page:>4} | {total:>7,} records | quota left {quota}", flush=True)
        if not payload.get("next") or not results:
            print(f"  gutenberg : done, {total:,} records")
            break
        if quota is not None and int(quota) <= QUOTA_FLOOR:
            print(f"  gutenberg : stopping, quota floor reached ({quota} left). Re-run to resume.")
            break
        page += 1
        time.sleep(GUT_SLEEP)

    out.close()
    write_manifest(gutenberg_records=total)
    return GUT_RAW


# -------------------------------------------------------------------- build

def _year(v):
    """rights_date_used doubles as a publication year, but 9999 means unknown."""
    v = (v or "").strip()
    if v.isdigit():
        n = int(v)
        if 1000 <= n <= 2026:
            return n
    return None


def _flush(writer, cols):
    writer.write_table(pa.Table.from_pydict(cols, schema=SCHEMA))
    for v in cols.values():
        v.clear()


def build_combined(force=False):
    """Stream both sources into one Parquet file, in batches so memory stays flat."""
    if os.path.exists(COMBINED) and not force:
        print(f"  combined  : cached ({os.path.getsize(COMBINED)/1e6:.0f} MB) - skipping")
        return COMBINED

    man = read_manifest()
    hathi_path = f"{DATA_DIR}/{man.get('hathi_file', '')}"
    if not os.path.exists(hathi_path):
        raise SystemExit("run --download first (no HathiTrust file cached)")

    cols = {f.name: [] for f in SCHEMA}
    writer = pq.ParquetWriter(COMBINED, SCHEMA, compression="zstd")
    n_h = n_g = 0
    t0 = time.time()

    print("  combined  : streaming HathiTrust ...")
    with gzip.open(hathi_path, "rt", encoding="utf-8", errors="replace") as fh:
        for f in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
            if len(f) < 26:
                continue
            n_h += 1
            cols["source"].append("hathitrust")
            cols["id"].append(f[0])
            cols["title"].append(f[11] or None)
            cols["author"].append(f[25] or None)
            cols["rights"].append(f[2] or None)
            cols["is_public_domain"].append(f[2] in PD_RIGHTS)
            cols["bib_fmt"].append(f[19] or None)
            cols["access"].append(f[1] or None)
            cols["language"].append(f[18] or None)
            cols["publication_year"].append(_year(f[16]))
            cols["publisher"].append(f[12] or None)
            cols["oclc_num"].append(f[7] or None)
            cols["isbn"].append(f[8] or None)
            cols["lccn"].append(f[10] or None)
            cols["subjects"].append(None)
            cols["summary"].append(None)
            cols["download_count"].append(None)
            cols["source_url"].append(f"https://babel.hathitrust.org/cgi/pt?id={f[0]}")
            if len(cols["id"]) >= BATCH:
                _flush(writer, cols)
                print(f"    ...{n_h:,} hathitrust rows", flush=True)

    print("  combined  : appending Gutenberg ...")
    with open(GUT_RAW, encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            n_g += 1
            authors = r.get("authors") or []
            cols["source"].append("gutenberg")
            cols["id"].append(str(r.get("id")))
            cols["title"].append(r.get("title") or None)
            cols["author"].append("; ".join(a["name"] for a in authors) or None)
            # Gutenberg exposes no per-record copyright field; its collection is
            # public domain by policy, so this is a collection-level assertion.
            cols["rights"].append("pd")
            cols["is_public_domain"].append(True)
            # Gutenberg has no bib_fmt either. Nearly everything it holds is a book,
            # so BK is asserted here to keep the column joinable across sources.
            cols["bib_fmt"].append("BK")
            cols["access"].append("allow")
            cols["language"].append(None)      # not exposed by the API
            cols["publication_year"].append(None)  # `issued` is the PG release date
            cols["publisher"].append(None)
            cols["oclc_num"].append(None)
            cols["isbn"].append(None)
            cols["lccn"].append(None)
            cols["subjects"].append("; ".join(r.get("subjects") or []) or None)
            cols["summary"].append(r.get("summary") or None)
            cols["download_count"].append(r.get("download_count"))
            cols["source_url"].append(f"https://www.gutenberg.org/ebooks/{r.get('id')}")
            if len(cols["id"]) >= BATCH:
                _flush(writer, cols)

    if cols["id"]:
        _flush(writer, cols)
    writer.close()

    size = os.path.getsize(COMBINED) / 1e6
    print(f"  combined  : {n_h + n_g:,} rows ({n_h:,} hathitrust + {n_g:,} gutenberg), "
          f"{size:.0f} MB, {time.time()-t0:.0f}s")
    write_manifest(combined_rows=n_h + n_g, combined_hathitrust=n_h,
                   combined_gutenberg=n_g, combined_built=time.strftime("%Y-%m-%d %H:%M"))
    return COMBINED


# -------------------------------------------------------------------- query

def load_combined(columns=None, source=None, bib_fmt=None, public_domain=None,
                  language=None, limit=None):
    """Load the combined table as a pandas DataFrame, filtering during the read.

    Filters are pushed into the Parquet reader, so `load_combined(source="gutenberg")`
    touches a fraction of the file instead of loading 19.7M rows and subsetting.
    """
    dataset = ds.dataset(COMBINED, format="parquet")
    expr = None

    def AND(a, b):
        return b if a is None else a & b

    if source is not None:
        expr = AND(expr, ds.field("source") == source)
    if bib_fmt is not None:
        expr = AND(expr, ds.field("bib_fmt") == bib_fmt)
    if public_domain is not None:
        expr = AND(expr, ds.field("is_public_domain") == public_domain)
    if language is not None:
        expr = AND(expr, ds.field("language") == language)

    table = dataset.to_table(columns=columns, filter=expr)
    if limit:
        table = table.slice(0, limit)
    return table.to_pandas()


# ---------------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--download", action="store_true", help="fetch both source datasets")
    ap.add_argument("--build", action="store_true", help="merge them into combined.parquet")
    ap.add_argument("--stats", action="store_true", help="summarize the combined table")
    ap.add_argument("--all", action="store_true", help="download + build + stats")
    ap.add_argument("--force", action="store_true", help="ignore caches and redo")
    args = ap.parse_args()
    if not any((args.download, args.build, args.stats, args.all)):
        ap.print_help()
        return 0

    os.makedirs(DATA_DIR, exist_ok=True)
    if args.download or args.all:
        print("DOWNLOAD")
        download_hathitrust(force=args.force)
        download_gutenberg(force=args.force)
    if args.build or args.all:
        print("BUILD")
        build_combined(force=args.force)
    if args.stats or args.all:
        print("STATS")
        t = ds.dataset(COMBINED, format="parquet")
        df = t.to_table(columns=["source", "bib_fmt", "is_public_domain"]).to_pandas()
        print(f"\n  rows: {len(df):,}")
        print("\n  by source:")
        print(df.source.value_counts().to_string())
        print("\n  by source x public domain:")
        print(df.groupby(["source", "is_public_domain"]).size().to_string())
        print("\n  by bib_fmt:")
        print(df.bib_fmt.value_counts().to_string())
        print(f"\n  manifest: {read_manifest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
