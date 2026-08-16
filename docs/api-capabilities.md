# gutenbergapi.com — verified capabilities

Everything here was confirmed against the live API on 2026-08-15 with a free-tier key.
Re-run `scripts/probe_api.py` after any plan change; it rewrites `api-capabilities.json`.

## The published documentation is wrong

gutenbergapi.com's examples give a host and path that do not exist. Both had to be
corrected by probing:

| | Published | Actually works |
|---|---|---|
| Host | `project-gutenberg-books-api.p.rapidapi.com` | `project-gutenberg-free-books-api1.p.rapidapi.com` |
| Path | `/api/books` | `/books` |

The two failure modes are distinguishable, which is what made this findable:

- `{"message":"API doesn't exists"}` with header `x-rapidapi-proxy-response: true`
  — the **RapidAPI proxy** rejected the `X-RapidAPI-Host`. Wrong host.
- `{"message":"Endpoint '/api/books' does not exist"}` — the **backend** rejected the
  path. Right host, wrong path.

## Verified behaviour

| Question | Answer |
|---|---|
| Lists the catalog with no search query? | Yes — `GET /books` returns everything, paginated |
| Default page size | 32 |
| Maximum `page_size` | **100**; larger values are silently capped, not rejected |
| Stable sort? | Yes — `sort=ascending` orders by Gutenberg ID |
| Total count in response? | No `count` field; page until `next` is null |
| Free tier quota | **1000 requests/month** (`x-ratelimit-requests-limit: 1000`) |
| Rate limiting | Aggressive. Back-to-back requests return 429; 12s spacing is comfortable |

`sort=ascending` matters more than it looks: new books get higher IDs and therefore land
at the **end** of a stable ordering. That is what makes page-number checkpointing valid.
`ordering=` / `order_by=` are silently ignored (they return the default popularity order).

## Fields in the list response

```
id, title, alternative_title, authors[{id,name}], subjects[], bookshelves[],
formats{mime: url}, download_count, issued, summary, reading_ease_score,
cover_image, removed_from_catalog
```

**`summary` is the important one.** It is populated for ~96.5% of records (193 of the first
200 sampled) and is a real editorial abstract of 600–1200 characters. It arrives with the
listing at **no extra request cost**, which makes it the spec's first-choice searchable text
and reduces the per-book `/books/{id}/text` endpoint to a fallback for the remaining ~3.5%.

`formats` carries real download URLs; the `text/plain` entry is preferred for `download_url`,
falling back to a URL derived from the Gutenberg ID.

## Fields that do not exist

No `language`, `publisher`, `media_type`, or ISBN anywhere in the response, and `issued` is
Gutenberg's own release date, not original publication (ID 1 is dated `1971-12-01`). These
stay null in the schema; see the README's "Known data gaps".

## Cost model

| Job | Requests |
|---|---|
| 1,000-book PoC | ~10 |
| Full catalog (~79k books) at `page_size=100` | ~790 |
| Preview text fallback | 1 per book lacking a summary (~3.5%) |

A full backfill fits inside a single month of the free tier — but only just, and only if the
text fallback is kept on a tight budget. Watch `quota left` in the run summary.

## The `/books/{id}/text` endpoint

Returns the whole cleaned text as one JSON field: book 1342 came back at **727 KB**. The
pipeline truncates locally to `preview.chars`. Note that `cleaning_mode=simple` does not strip
front matter reliably — the response for 1342 began with publisher plate text — so the provider
also strips through the Gutenberg `*** START OF THE ... ***` marker when present.
