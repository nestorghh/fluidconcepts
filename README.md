# pd-books

A small, modular pipeline that collects **public domain book metadata** from external
providers and stores it as Parquet, so downstream ML workflows can search a known-public-domain
catalog and avoid reprocessing books that are already freely available.

Currently ships one provider — Project Gutenberg via [gutenbergapi.com](https://gutenbergapi.com) —
and two storage backends (local filesystem, Amazon S3).

## Quick start

```bash
uv venv --python 3.14
uv pip install -e ".[s3,dev]"

export RAPIDAPI_KEY=your-key-here      # required; never put the key in config.yaml
python -m pd_books --config config.yaml
```

Useful flags:

```bash
python -m pd_books --dry-run                 # fetch + normalize, write nothing, spend no storage
python -m pd_books --stage metadata          # skip preview text entirely
python -m pd_books --mode full               # rescan the catalog from page 1
python -m pd_books --max-books 20            # small smoke run
```

## How a run works

Each invocation is one run. Two stages, each with its own budget:

1. **metadata** — pages the provider catalog and normalizes records into the canonical schema.
   The listing includes an editorial `summary` for ~96.5% of books, so most records get their
   searchable text here at **no extra request cost**.
2. **text** — a fallback for the ~3.5% with no summary. Spends a separate request budget,
   **highest `download_count` first**, so the most-searched books get text first.

`--max-books` is honoured to the **end of the page it lands on**, so a run can overshoot the cap
by up to `page_size - 1` records. That is deliberate: the page checkpoint only advances on a fully
read page, and stopping mid-page would make every later run re-read all earlier pages.

Running out of request budget or API quota is a **clean stop, not a failure** (exit code 0). The run
checkpoints and the next invocation resumes. That makes the pipeline safe to drive from cron,
GitHub Actions, or Airflow on any schedule.

```bash
# weekly, Sundays at 03:00
0 3 * * 0 cd /path/to/pd-books && .venv/bin/python -m pd_books --config config.yaml
```

## Configuration

Everything is config-driven; nothing is hardcoded. See `config.yaml`.

| Key | Meaning |
|---|---|
| `run.mode` | `incremental` (resume from checkpoint) or `full` (rescan from page 1) |
| `run.max_books` | Cap on records collected per run |
| `run.max_requests_per_run` | Hard cap on API requests per run |
| `storage.backend` | `local` or `s3` |
| `storage.local_path` / `s3_bucket` / `s3_prefix` | Where data lands |
| `storage.rows_per_shard` | Records per Parquet file |
| `sources[].page_size` | Catalog page size (**100 is the verified maximum**) |
| `sources[].api_key_env` | **Name of the env var** holding the API key |
| `sources[].min_request_interval` | Seconds between requests; the free plan 429s without it |
| `sources[].params` | Provider-specific query params, e.g. `{sort: ascending}` |
| `preview.chars` | Preview text length (spec range 3,000–5,000) |
| `preview.text_requests_per_run` | Sub-budget for the text stage |

Any value can be overridden by environment variable using `PDBOOKS__SECTION__KEY`:

```bash
PDBOOKS__STORAGE__BACKEND=s3 PDBOOKS__RUN__MAX_BOOKS=5000 python -m pd_books
```

## Storage layout

Identical on local disk and S3:

```
books/provider=gutenberg/ingest_date=2026-08-15/part-0000.parquet
_state/gutenberg.json          # checkpoint: last page, seen hashes, pending text, quota
_manifest/2026-08-15-gutenberg.json
```

Hive-style partitioning means the whole corpus reads in one call:

```python
import pyarrow.dataset as ds
table = ds.dataset("./data/books/books", partitioning="hive").to_table()
```

**Writes are append-only.** A record that changes across runs is written again with a newer
`ingested_at`. For the deduplicated latest-wins view, use the helper:

```python
from pd_books.storage.parquet import read_catalog
books = read_catalog(storage, "gutenberg")
```

## Schema and the downstream ML use case

`book_uid` (e.g. `gutenberg:1342`) is a **stable primary key**, unique across providers.

**Embeddings are deliberately not stored in this table.** Build a vector index separately and join
it on `book_uid`. That is what lets you add embeddings, or swap embedding models, without touching
the ingestion pipeline or rewriting the corpus. `schema_version` plus an append-only column policy
(never retype or remove an existing field) covers the rest of schema evolution.

`searchable_text` carries the preview text and `text_source` records where it came from
(`summary` › `description` › `intro` › `first_chars` › `none`), so a downstream consumer can weight
or filter by text quality. In practice ~96.5% of records land on `summary` — a real editorial
abstract of 600–1200 characters, which is well suited to embedding directly.

`withdrawn_reason` is set when the source has pulled a work (e.g. a later copyright claim).
Those records are kept rather than silently dropped; **filter them out** when building a
public-domain corpus.

## Known data gaps

These fields are in the schema but come back **null for essentially every Gutenberg record**:

| Field | Why |
|---|---|
| `publication_year` | Gutenberg records its own release date, not original publication. *Pride and Prejudice* is dated `1998-06-01`, not 1813. Verified against Gutenberg's own RDF metadata. |
| `language`, `publisher`, `media_type` | Not present anywhere in the API response (verified live). |
| ISBN | Gutenberg books do not carry ISBNs. |

This is a limit of the source, not a bug. The fields exist so a richer provider can populate them —
**Open Library** is the natural next one, since it has both publication year and ISBNs.

One caveat on rights: `is_public_domain` is set from Project Gutenberg's collection-level status
because the API exposes no per-record copyright flag. `rights_statement` records that provenance.
Treat it as a strong signal, not legal verification.

## Adding a provider

The pipeline never imports a concrete provider — sources are resolved by name from config.

1. Create `pd_books/providers/<name>.py`.
2. Subclass `BaseProvider`, decorate with `@register_provider`, set `name` and `version`.
3. Implement `iter_records()` (paging + checkpointing) and `to_book()` (normalize to `Book`).
   Optionally implement `fetch_preview_text()`.
4. Add an entry under `sources:` in `config.yaml`.

No pipeline or storage changes are required.

## Tests

```bash
.venv/bin/python -m pytest -q
```

71 tests, all offline — provider normalization runs against recorded JSON fixtures, and the
pipeline tests drive a fake provider. Nothing in the suite touches the network.

## API quirks and cost

The vendor's published docs give a **host and path that do not exist**. The working values are in
`config.yaml` and the evidence is in [`docs/api-capabilities.md`](docs/api-capabilities.md).

Free tier is **1000 requests/month**, and the gateway 429s on back-to-back requests — hence
`min_request_interval: 12`. Budget accordingly:

| Job | Requests |
|---|---|
| 1,000-book PoC | ~10 |
| Full catalog (~79k) at `page_size: 100` | ~790 |
| Text fallback | 1 per book without a summary (~3.5%) |

Every run prints `quota left` from the API's own headers. Re-run `scripts/probe_api.py` after
changing plans to refresh the verified numbers.

Incremental resume relies on `sort: ascending` (verified). New books get higher Gutenberg IDs and
land at the end of a stable ordering, which is what makes page checkpointing valid. Do not remove
that param without switching to `run.mode: full`.
